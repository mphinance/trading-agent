"""Unit tests for Active Position Monitor & Exit Cascade Loop (Module 3)."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from vesper.monitor import PositionMonitor, MonitoredPosition, ExitTrigger
from vesper.state import OrderProposal, ExecutionResult


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

    with patch("wb.Webull", return_value=mock_wb):
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
    with patch("wb.Webull", return_value=mock_wb):
        res = await monitor.execute_exit_cascade(trigger, live=True)

    assert res.status == "BLOCKED_BY_GUARDRAIL"
    mock_wb.trade.order_v2.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_exit_cascade_closes_paper_position(tmp_path, monkeypatch):
    """Verify dry-run exit cascade closes open position in paper ledger."""
    from vesper.paper_ledger import record_paper_fill, get_paper_positions
    fake_ledger = tmp_path / "paper_ledger.json"
    monkeypatch.setattr("vesper.paper_ledger._LEDGER_PATH", fake_ledger)

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
    from vesper.paper_ledger import _load_ledger, get_paper_summary
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

