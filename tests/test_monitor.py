"""Unit tests for Active Position Monitor & Exit Cascade Loop (Module 3)."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from vesper.monitor import PositionMonitor, MonitoredPosition, ExitTrigger
from vesper.state import OrderProposal, ExecutionResult
from vesper.metrics import metrics


@pytest.fixture(autouse=True)
def _clean_guard_env(monkeypatch):
    for var in ("VESPER_TRADING", "VESPER_MAX_NOTIONAL", "VESPER_MAX_QUANTITY", "VESPER_SYMBOL_ALLOWLIST"):
        monkeypatch.delenv(var, raising=False)


def test_take_profit_trigger():
    monitor = PositionMonitor()
    # Entry $100 -> Current $155 (+55% gain)
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=155.0)
    trigger = monitor.evaluate_position(pos)
    assert trigger is not None
    assert trigger.reason == "TAKE_PROFIT"
    assert trigger.sell_quantity == 10
    assert trigger.est_proceeds == 1550.0
    assert trigger.pnl_pct == 0.55


def test_stop_loss_trigger():
    monitor = PositionMonitor()
    # Entry $100 -> Current $55 (-45% loss)
    pos = MonitoredPosition(symbol="TSLA", quantity=5, entry_price=100.0, current_price=55.0)
    trigger = monitor.evaluate_position(pos)
    assert trigger is not None
    assert trigger.reason == "STOP_LOSS"
    assert trigger.urgency == "CRITICAL"
    assert trigger.pnl_pct == -0.45


def test_trailing_breakeven_lock_and_stop():
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="AMD", quantity=10, entry_price=100.0, current_price=128.0)
    # First tick: +28% gain -> Should activate breakeven lock, but NOT trigger exit
    trigger1 = monitor.evaluate_position(pos)
    assert trigger1 is None
    assert pos.breakeven_locked is True

    # Second tick: Price drops back to $99 (-1%) -> Should trigger BREAKEVEN_STOP
    pos.current_price = 99.0
    trigger2 = monitor.evaluate_position(pos)
    assert trigger2 is not None
    assert trigger2.reason == "BREAKEVEN_STOP"
    assert trigger2.sell_quantity == 10


def test_0dte_time_stop_trigger():
    monitor = PositionMonitor()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pos = MonitoredPosition(
        symbol="SPY",
        quantity=2,
        entry_price=2.00,
        current_price=2.10,
        asset_type="OPTION",
        expiry=today_str,
    )
    assert pos.is_0dte is True

    # 14:30 ET -> Within trading window, no time stop
    time_early = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
    trigger_early = monitor.evaluate_position(pos, current_time_et=time_early)
    assert trigger_early is None

    # 15:05 ET -> Exceeded 15:00 ET cutoff -> Triggers TIME_STOP
    time_late = datetime(2026, 8, 28, 15, 5, tzinfo=timezone.utc)
    trigger_late = monitor.evaluate_position(pos, current_time_et=time_late)
    assert trigger_late is not None
    assert trigger_late.reason == "TIME_STOP"
    assert trigger_late.sell_quantity == 2


def test_gamma_flip_violation_trigger():
    monitor = PositionMonitor()
    # SPY Call bought at $3.00 premium, currently $2.80 (-6.6% loss), SPY spot is $570, but Gamma Flip is at $575 -> Violation
    pos = MonitoredPosition(
        symbol="SPY",
        quantity=1,
        entry_price=3.00,
        current_price=2.80,
        asset_type="OPTION",
        option_type="CALL",
    )
    trigger = monitor.evaluate_position(pos, spy_spot=570.0, spy_gamma_flip=575.0)
    assert trigger is not None
    assert trigger.reason == "GAMMA_FLIP_VIOLATION"


def test_healthy_position_no_trigger():
    monitor = PositionMonitor()
    # +10% gain -> Healthy, no trigger
    pos = MonitoredPosition(symbol="AAPL", quantity=20, entry_price=200.0, current_price=220.0)
    trigger = monitor.evaluate_position(pos)
    assert trigger is None


# ---------------------------------------------------------------------------
# Underlying-keyed swing-option stop (200 SMA / 34 EMA / lower Keltner band)
# ---------------------------------------------------------------------------

def _swing_call_pos(current_price=6.00, entry_price=6.00, basis="ema_34"):
    """A swing LEAPS call, comfortably inside the -40% flat stop band, so any
    trigger observed can only be the underlying-level stop firing."""
    return MonitoredPosition(
        symbol="MSFT",
        quantity=1,
        entry_price=entry_price,
        current_price=current_price,
        asset_type="OPTION",
        option_type="call",
        underlying_stop_type="underlying_level",
        underlying_stop_basis=basis,
    )


def test_underlying_level_stop_fires_for_swing_call_breach():
    monitor = PositionMonitor()
    pos = _swing_call_pos()
    # pnl_pct is 0.0 -- nowhere near the -40% stop-loss band, proving this is
    # a genuinely independent trigger.
    tech = {"close": 395.0, "ema_34": 400.0}  # underlying close < basis level
    trigger = monitor.evaluate_position(pos, underlying_technicals=tech)
    assert trigger is not None
    assert trigger.reason == "UNDERLYING_LEVEL_STOP"
    assert trigger.urgency == "CRITICAL"
    assert abs(pos.unrealized_pnl_pct) < 0.40


def test_underlying_level_stop_skips_when_technicals_unavailable():
    """Simulates analyze_technicals() failing/being unavailable this cycle --
    even though the same close/level pair would have breached had data been
    present, a None read must be treated as skip, not fabricated as pass."""
    monitor = PositionMonitor()
    pos = _swing_call_pos()
    trigger = monitor.evaluate_position(pos, underlying_technicals=None)
    assert trigger is None


def test_underlying_level_stop_skips_when_not_drafted_with_swing_stop():
    """A legacy/non-swing position (underlying_stop_type is None) must never
    have this check apply, even given a breaching underlying_technicals dict."""
    monitor = PositionMonitor()
    pos = MonitoredPosition(
        symbol="MSFT",
        quantity=1,
        entry_price=6.00,
        current_price=6.00,
        asset_type="OPTION",
        option_type="call",
        underlying_stop_type=None,
        underlying_stop_basis=None,
    )
    tech = {"close": 300.0, "ema_34": 400.0}  # would clearly breach if checked
    trigger = monitor.evaluate_position(pos, underlying_technicals=tech)
    assert trigger is None


def test_underlying_level_stop_skips_when_basis_key_missing():
    """basis='sma_200' but the dict only carries ema_34/keltner_lower (e.g. a
    short-history ticker where analyze_technicals couldn't compute SMA200) --
    a missing key must be treated as unavailable, never as 0.0."""
    monitor = PositionMonitor()
    pos = _swing_call_pos(basis="sma_200")
    tech = {"close": 100.0, "ema_34": 150.0, "keltner_lower": 140.0}  # no sma_200 key
    trigger = monitor.evaluate_position(pos, underlying_technicals=tech)
    assert trigger is None


def test_underlying_level_stop_put_side_breach_direction():
    monitor = PositionMonitor()
    put_pos = MonitoredPosition(
        symbol="MSFT",
        quantity=1,
        entry_price=6.00,
        current_price=6.00,
        asset_type="OPTION",
        option_type="put",
        underlying_stop_type="underlying_level",
        underlying_stop_basis="ema_34",
    )
    # Put breach direction is the mirror of a call: fires when the underlying
    # rises ABOVE the basis level.
    trigger_breach = monitor.evaluate_position(put_pos, underlying_technicals={"close": 410.0, "ema_34": 400.0})
    assert trigger_breach is not None
    assert trigger_breach.reason == "UNDERLYING_LEVEL_STOP"

    put_pos2 = MonitoredPosition(
        symbol="MSFT",
        quantity=1,
        entry_price=6.00,
        current_price=6.00,
        asset_type="OPTION",
        option_type="put",
        underlying_stop_type="underlying_level",
        underlying_stop_basis="ema_34",
    )
    trigger_no_breach = monitor.evaluate_position(put_pos2, underlying_technicals={"close": 390.0, "ema_34": 400.0})
    assert trigger_no_breach is None


def test_poll_paper_positions_populates_underlying_stop_fields(tmp_path, monkeypatch):
    """End-to-end: a paper_ledger.json fill entry carrying
    underlying_stop_type/underlying_stop_basis produces a MonitoredPosition
    with those fields populated."""
    from core.paper_ledger import record_paper_fill
    fake_ledger = tmp_path / "paper_ledger.json"
    monkeypatch.setattr("core.paper_ledger._LEDGER_PATH", fake_ledger)

    proposal = OrderProposal(
        id="prop-test-msft-leaps",
        ticker="MSFT",
        asset_type="OPTION",
        side="BUY",
        limit_price=18.50,
        quantity=1,
        strike=440.0,
        option_type="call",
        underlying_stop_type="underlying_level",
        underlying_stop_basis="ema_34",
    )
    result = ExecutionResult(
        order_proposal_id="prop-test-msft-leaps",
        ticker="MSFT",
        status="DRY_RUN_SIMULATED",
        filled_quantity=1,
        filled_price=18.50,
    )
    record_paper_fill(proposal, result, session_id="sess-test")

    monitor = PositionMonitor()
    positions = monitor.poll_paper_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.symbol == "MSFT"
    assert pos.underlying_stop_type == "underlying_level"
    assert pos.underlying_stop_basis == "ema_34"


@pytest.mark.asyncio
async def test_execute_exit_cascade_dry_run():
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=160.0)
    trigger = ExitTrigger(
        position=pos,
        reason="TAKE_PROFIT",
        sell_quantity=10,
        est_proceeds=1600.0,
        pnl_pct=0.60,
    )
    res = await monitor.execute_exit_cascade(trigger, live=False)
    assert res.status == "DRY_RUN_SIMULATED"
    assert res.ticker == "NVDA"
    assert res.filled_quantity == 10
    assert res.filled_price == 160.0


@pytest.mark.asyncio
async def test_execute_exit_cascade_live_places_guarded_order(monkeypatch):
    """Pins the guard call signature — this is a real place_order call behind
    ExecutionGuard, not the old broken guard.preview(symbol=..., ...)/ticket.ok
    shape that would have raised TypeError before ever reaching the broker."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=160.0)
    trigger = ExitTrigger(
        position=pos, reason="TAKE_PROFIT", sell_quantity=10, est_proceeds=1600.0, pnl_pct=0.60,
    )

    mock_wb = MagicMock()
    mock_wb.accounts.return_value = [{"account_id": "ACC1"}]
    mock_wb.portfolio.return_value = {"totals": {"buying_power": 100000.0}}
    mock_wb.trade.order_v2.place_order.return_value = {"data": {"order_id": "ORD123"}}

    with patch("core.wb.Webull", return_value=mock_wb):
        res = await monitor.execute_exit_cascade(trigger, live=True)

    assert res.status == "SUBMITTED"
    mock_wb.trade.order_v2.place_order.assert_called_once()
    call_kwargs = mock_wb.trade.order_v2.place_order.call_args.kwargs
    assert call_kwargs["account_id"] == "ACC1"
    assert call_kwargs["stock_order_sub_request"]["action"] == "SELL"
    assert call_kwargs["stock_order_sub_request"]["quantity"] == 10


@pytest.mark.asyncio
async def test_execute_exit_cascade_live_blocked_by_kill_switch(monkeypatch):
    """VESPER_TRADING defaults off — a live exit trigger must not reach the
    broker at all, and must report BLOCKED_BY_GUARDRAIL rather than crash."""
    monkeypatch.delenv("VESPER_TRADING", raising=False)
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=55.0)
    trigger = ExitTrigger(
        position=pos, reason="STOP_LOSS", sell_quantity=10, est_proceeds=550.0, pnl_pct=-0.45,
    )

    mock_wb = MagicMock()
    with patch("core.wb.Webull", return_value=mock_wb):
        res = await monitor.execute_exit_cascade(trigger, live=True)

    assert res.status == "BLOCKED_BY_GUARDRAIL"
    mock_wb.trade.order_v2.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_exit_cascade_closes_paper_position(tmp_path, monkeypatch):
    """Verify dry-run exit cascade closes open position in paper ledger."""
    from core.paper_ledger import record_paper_fill, get_paper_positions
    fake_ledger = tmp_path / "paper_ledger.json"
    monkeypatch.setattr("core.paper_ledger._LEDGER_PATH", fake_ledger)

    # Open a simulated paper position
    test_prop = OrderProposal(
        id="prop-test-nvda",
        ticker="NVDA",
        asset_type="EQUITY",
        side="BUY",
        limit_price=100.0,
        quantity=10,
    )
    test_res = ExecutionResult(
        order_proposal_id="prop-test-nvda",
        ticker="NVDA",
        status="DRY_RUN_SIMULATED",
        filled_quantity=10,
        filled_price=100.0,
    )
    open_fill = record_paper_fill(test_prop, test_res, session_id="sess-test")
    assert open_fill["status"] == "OPEN"

    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=150.0)
    trigger = ExitTrigger(
        position=pos,
        reason="TAKE_PROFIT",
        sell_quantity=10,
        est_proceeds=1500.0,
        pnl_pct=0.50,
    )

    res = await monitor.execute_exit_cascade(trigger, live=False)
    assert res.status == "DRY_RUN_SIMULATED"

    # Verify paper ledger position is now CLOSED
    from core.paper_ledger import _load_ledger, get_paper_summary
    open_positions = get_paper_positions()
    assert len(open_positions) == 0

    ledger_data = _load_ledger()
    closed_fills = [f for f in ledger_data.get("fills", []) if f["id"] == open_fill["id"]]
    assert len(closed_fills) == 1
    assert closed_fills[0]["status"] == "CLOSED"
    assert closed_fills[0]["close_price"] == 150.0
    assert closed_fills[0]["realized_pnl"] == 500.0

    summary = get_paper_summary()
    assert summary["closed_trades_count"] == 1
    assert summary["realized_pnl"] == 500.0



# ── Earnings-Week CSP Vega Harvest exit (date-driven, not P&L-driven) ───────

def test_earnings_exit_fires_on_exit_date_regardless_of_pnl():
    monitor = PositionMonitor()
    # A SHORT put marked as a modest LOSS (short options gain when price
    # rises against them in unrealized_pnl_pct terms is irrelevant here --
    # what matters is the exit fires purely off the date, not this number).
    pos = MonitoredPosition(
        symbol="MDB", quantity=1, entry_price=3.00, current_price=3.05,
        asset_type="OPTION", option_type="PUT", earnings_exit_date="2026-09-02",
    )
    trigger = monitor.evaluate_position(pos, current_time_et=datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc))
    assert trigger is not None
    assert trigger.reason == "EARNINGS_EXIT"
    assert trigger.sell_quantity == 1


def test_earnings_exit_fires_after_exit_date_too():
    monitor = PositionMonitor()
    pos = MonitoredPosition(
        symbol="MDB", quantity=1, entry_price=3.00, current_price=3.05,
        asset_type="OPTION", option_type="PUT", earnings_exit_date="2026-09-02",
    )
    trigger = monitor.evaluate_position(pos, current_time_et=datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc))
    assert trigger is not None
    assert trigger.reason == "EARNINGS_EXIT"


def test_earnings_exit_does_not_fire_before_exit_date():
    monitor = PositionMonitor()
    pos = MonitoredPosition(
        symbol="MDB", quantity=1, entry_price=3.00, current_price=3.05,
        asset_type="OPTION", option_type="PUT", earnings_exit_date="2026-09-02",
    )
    trigger = monitor.evaluate_position(pos, current_time_et=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc))
    assert trigger is None


def test_earnings_exit_absent_for_positions_without_the_tag():
    """A normal position (earnings_exit_date=None, the dataclass default)
    must never spuriously trigger EARNINGS_EXIT."""
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="AAPL", quantity=1, entry_price=3.00, current_price=3.05, asset_type="OPTION")
    trigger = monitor.evaluate_position(pos, current_time_et=datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert trigger is None


def test_earnings_exit_malformed_date_skips_without_crashing():
    monitor = PositionMonitor()
    pos = MonitoredPosition(
        symbol="MDB", quantity=1, entry_price=3.00, current_price=3.05,
        asset_type="OPTION", option_type="PUT", earnings_exit_date="not-a-date",
    )
    # Must not raise -- fails closed (skips this check) rather than crashing the cycle.
    trigger = monitor.evaluate_position(pos, current_time_et=datetime(2026, 9, 5, tzinfo=timezone.utc))
    assert trigger is None


def test_poll_paper_positions_wires_earnings_exit_date(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("core.paper_ledger._DATA_DIR", data_dir)
    monkeypatch.setattr("core.paper_ledger._LEDGER_PATH", data_dir / "paper_ledger.json")

    from core.paper_ledger import record_paper_fill
    from vesper.state import ExecutionResult, OrderProposal

    prop = OrderProposal(
        id="prop-earnvega-abc123", ticker="MDB", asset_type="OPTION", side="SELL",
        limit_price=3.00, quantity=1, strike=280.0, option_type="put",
        earnings_exit_date="2026-09-02",
    )
    result = ExecutionResult(order_proposal_id=prop.id, ticker="MDB", status="DRY_RUN_SIMULATED",
                              filled_quantity=1, filled_price=3.00)
    record_paper_fill(proposal=prop, result=result)

    monitor = PositionMonitor()
    positions = monitor.poll_paper_positions()
    assert len(positions) == 1
    assert positions[0].earnings_exit_date == "2026-09-02"


# -- metrics.py instrumentation --------------------------------------------


@pytest.mark.asyncio
async def test_execute_exit_cascade_dry_run_records_order_outcome():
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=160.0)
    trigger = ExitTrigger(position=pos, reason="TAKE_PROFIT", sell_quantity=10, est_proceeds=1600.0, pnl_pct=0.60)
    await monitor.execute_exit_cascade(trigger, live=False)
    snap = metrics.snapshot()["order_outcomes"]["paper"]["webull"]
    assert snap == {"DRY_RUN_SIMULATED": 1}
    # digest is over the symbol only, never the fill quantity/price
    digest = metrics.snapshot()["recent_order_digests"][-1]
    assert digest["mode"] == "paper" and digest["status"] == "DRY_RUN_SIMULATED"
    assert "160.0" not in digest["payload_digest"] and "NVDA" not in digest["payload_digest"]


@pytest.mark.asyncio
async def test_execute_exit_cascade_live_submitted_records_order_outcome(monkeypatch):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=160.0)
    trigger = ExitTrigger(position=pos, reason="TAKE_PROFIT", sell_quantity=10, est_proceeds=1600.0, pnl_pct=0.60)

    mock_wb = MagicMock()
    mock_wb.accounts.return_value = [{"account_id": "ACC1"}]
    mock_wb.portfolio.return_value = {"totals": {"buying_power": 100000.0}}
    mock_wb.trade.order_v2.place_order.return_value = {"data": {"order_id": "ORD123"}}

    with patch("core.wb.Webull", return_value=mock_wb):
        await monitor.execute_exit_cascade(trigger, live=True)

    snap = metrics.snapshot()["order_outcomes"]["live"]["webull"]
    assert snap == {"SUBMITTED": 1}


@pytest.mark.asyncio
async def test_execute_exit_cascade_blocked_by_kill_switch_records_order_outcome(monkeypatch):
    monkeypatch.delenv("VESPER_TRADING", raising=False)
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=55.0)
    trigger = ExitTrigger(position=pos, reason="STOP_LOSS", sell_quantity=10, est_proceeds=550.0, pnl_pct=-0.45)

    mock_wb = MagicMock()
    with patch("core.wb.Webull", return_value=mock_wb):
        await monitor.execute_exit_cascade(trigger, live=True)

    snap = metrics.snapshot()["order_outcomes"]["live"]["webull"]
    assert snap == {"BLOCKED_BY_GUARDRAIL": 1}


@pytest.mark.asyncio
async def test_execute_exit_cascade_broker_exception_records_failed_outcome(monkeypatch):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=160.0)
    trigger = ExitTrigger(position=pos, reason="TAKE_PROFIT", sell_quantity=10, est_proceeds=1600.0, pnl_pct=0.60)

    with patch("core.wb.Webull", side_effect=RuntimeError("connection refused")):
        res = await monitor.execute_exit_cascade(trigger, live=True)

    assert res.status == "FAILED"
    snap = metrics.snapshot()["order_outcomes"]["live"]["webull"]
    assert snap == {"FAILED": 1}


@pytest.mark.asyncio
async def test_run_monitoring_cycle_updates_status_timing():
    """Wraps _run_monitoring_cycle_impl -- status() should report a cycle
    count and a duration even when there are no positions to evaluate."""
    monitor = PositionMonitor()
    assert monitor.status() == {
        "cycles": 0, "last_cycle_at": None, "last_cycle_error": None,
        "tracked_positions": 0, "last_cycle_duration_sec": None, "avg_cycle_duration_sec": None,
    }

    with patch.object(monitor, "poll_webull_positions", AsyncMock(return_value=[])):
        await monitor.run_monitoring_cycle(live=True)

    st = monitor.status()
    assert st["cycles"] == 1
    assert st["last_cycle_at"] is not None
    assert st["last_cycle_error"] is None
    assert st["last_cycle_duration_sec"] is not None and st["last_cycle_duration_sec"] >= 0.0


@pytest.mark.asyncio
async def test_run_monitoring_cycle_records_error_in_status_and_still_reraises():
    """A cycle that raises must still update cycle count/timing (finally),
    record the error string, and propagate the exception -- run_monitor_loop
    is what catches it, not this method."""
    monitor = PositionMonitor()

    with patch.object(monitor, "poll_webull_positions", AsyncMock(side_effect=RuntimeError("wb down"))):
        with pytest.raises(RuntimeError, match="wb down"):
            await monitor.run_monitoring_cycle(live=True)

    st = monitor.status()
    assert st["cycles"] == 1
    assert st["last_cycle_error"] == "wb down"
