"""Unit tests for multi-leg (combo) order support: OrderProposal.legs,
paper_ledger's per-leg fill recording. execution_guard's SYNTHETIC_LONG risk
formula is covered separately in tests/test_execution_guard.py, and the
ADX/IV router's drafting of a Synthetic Long combo is covered in
tests/test_adx_iv_router.py. This file covers the remaining piece: booking
each leg's own cash impact and fill record once a combo proposal fills,
instead of the top-level proposal fields (which only describe one leg).
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from vesper.execution_guard import GuardError
from vesper.nodes.executor import _execute_webull_multileg
from core.paper_ledger import (
    _load_ledger,
    _save_ledger,
    get_paper_summary,
    record_paper_fill,
)
from vesper.state import ExecutionResult, OrderLeg, OrderProposal


@pytest.fixture
def clean_paper_ledger(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = data_dir / "paper_ledger.json"
    monkeypatch.setattr("core.paper_ledger._DATA_DIR", data_dir)
    monkeypatch.setattr("core.paper_ledger._LEDGER_PATH", ledger_path)
    return ledger_path


def _synthetic_long_proposal() -> OrderProposal:
    return OrderProposal(
        id="prop-adxiv-synth-abc123",
        ticker="NVDA",
        asset_type="OPTION",
        side="BUY",
        limit_price=8.50,
        strike=120.0,
        expiry="2025-09-19",
        option_type="call",
        strategy_type="SYNTHETIC_LONG",
        legs=[
            OrderLeg(side="BUY", option_type="call", strike=120.0, expiry="2025-09-19",
                     quantity=1, limit_price=8.50),
            OrderLeg(side="SELL", option_type="put", strike=120.0, expiry="2025-09-19",
                     quantity=1, limit_price=6.20),
        ],
        estimated_cost=12000.0,
        max_risk=12000.0,
    )


def test_multileg_fill_records_both_legs_separately(clean_paper_ledger):
    prop = _synthetic_long_proposal()
    result = ExecutionResult(
        order_proposal_id=prop.id, ticker=prop.ticker, status="DRY_RUN_SIMULATED",
        filled_quantity=prop.quantity, filled_price=prop.limit_price,
    )

    record_paper_fill(proposal=prop, result=result, session_id="sess-1")

    ledger = _load_ledger()
    fills = [f for f in ledger["fills"] if f["order_proposal_id"] == prop.id]
    assert len(fills) == 2

    call_fill = next(f for f in fills if f["option_type"] == "call")
    put_fill = next(f for f in fills if f["option_type"] == "put")
    assert call_fill["side"] == "BUY"
    assert call_fill["filled_price"] == 8.50
    assert call_fill["total_cost"] == 850.0  # 8.50 * 1 * 100
    assert put_fill["side"] == "SELL"
    assert put_fill["filled_price"] == 6.20
    assert put_fill["total_cost"] == 620.0
    assert put_fill["strategy_type"] == "SYNTHETIC_LONG"


def test_multileg_fill_nets_correct_cash_impact(clean_paper_ledger):
    """BUY call debits cash, SELL put credits cash -- net impact is the
    net debit/credit of the combo, not the call leg's cost alone."""
    ledger = _load_ledger()
    ledger["account"]["cash"] = 100000.0
    _save_ledger(ledger)

    prop = _synthetic_long_proposal()
    result = ExecutionResult(
        order_proposal_id=prop.id, ticker=prop.ticker, status="DRY_RUN_SIMULATED",
        filled_quantity=prop.quantity, filled_price=prop.limit_price,
    )
    record_paper_fill(proposal=prop, result=result)

    summary = get_paper_summary()
    # -850 (BUY call) + 620 (SELL put) = -230 net debit
    assert summary["cash"] == 100000.0 - 230.0


def test_multileg_fill_counts_as_two_open_positions(clean_paper_ledger):
    prop = _synthetic_long_proposal()
    result = ExecutionResult(
        order_proposal_id=prop.id, ticker=prop.ticker, status="DRY_RUN_SIMULATED",
        filled_quantity=prop.quantity, filled_price=prop.limit_price,
    )
    record_paper_fill(proposal=prop, result=result)

    summary = get_paper_summary()
    assert summary["open_positions_count"] == 2


def test_single_leg_proposal_unaffected_by_multileg_path(clean_paper_ledger):
    """A plain single-leg proposal (no .legs) must still take the original
    single-fill path -- regression guard for the new early-return branch."""
    prop = OrderProposal(
        id="prop-plain-1", ticker="AAPL", asset_type="EQUITY",
        side="BUY", quantity=10, limit_price=200.0,
    )
    result = ExecutionResult(
        order_proposal_id=prop.id, ticker=prop.ticker, status="DRY_RUN_SIMULATED",
        filled_quantity=10, filled_price=200.0,
    )
    record_paper_fill(proposal=prop, result=result)

    ledger = _load_ledger()
    fills = [f for f in ledger["fills"] if f["order_proposal_id"] == prop.id]
    assert len(fills) == 1
    assert fills[0]["total_cost"] == 2000.0


# ── Live executor path: _execute_webull_multileg ────────────────────────────
# Wire shape verified live 2026-08-29 against preview_option
# (docs/WEBULL_ORDER_PAYLOADS.md). The earlier version of these tests pinned a
# payload Webull actually rejects -- legs keyed by contract_symbol, combo_type
# carrying the strategy name -- so they have been rewritten against the
# verified shape and the refusals that replaced the unverified paths.


def _thega_proposal_for_refusal() -> OrderProposal:
    """Thega: 100 shares + 1 covered call + 3 CSPs -- a MIXED equity+option combo."""
    return OrderProposal(
        id="prop-thega-refuse", ticker="GME", asset_type="EQUITY", side="BUY",
        limit_price=50.0, quantity=100, strategy_type="THEGA",
        legs=[
            OrderLeg(side="BUY", asset_type="EQUITY", quantity=100, limit_price=50.0),
            OrderLeg(side="SELL", asset_type="OPTION", option_type="call", strike=50.0,
                     expiry="2026-12-18", quantity=1, limit_price=1.5),
            OrderLeg(side="SELL", asset_type="OPTION", option_type="put", strike=50.0,
                     expiry="2026-12-18", quantity=3, limit_price=1.2),
        ],
        estimated_cost=20000.0, max_risk=20000.0,
    )


def _single_option_leg_proposal() -> OrderProposal:
    """One option leg -- the only combo shape verified against Webull.

    NOTE: no playbook currently drafts this. Both real legged strategies
    (SYNTHETIC_LONG, THEGA) are refused below on evidence grounds, so
    _execute_webull_multileg has no reachable success path in production
    today. This fixture exercises the wire-shape translation itself, which is
    the part the live preview verified."""
    return OrderProposal(
        id="prop-single-opt", ticker="SPY", asset_type="OPTION", side="BUY",
        limit_price=8.50, quantity=1, strike=770.0, expiry="2026-12-18",
        option_type="call", strategy_type="SINGLE_OPTION",
        legs=[OrderLeg(side="BUY", asset_type="OPTION", option_type="call",
                       strike=770.0, expiry="2026-12-18", quantity=1, limit_price=8.50)],
        estimated_cost=850.0, max_risk=850.0,
    )


@pytest.mark.asyncio
async def test_single_option_leg_uses_the_verified_wire_shape(monkeypatch):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "100000")
    # Register a formula for the fixture's strategy. The whitelist is a
    # deliberate separate policy (an unregistered strategy is refused, pinned
    # in test_execution_guard.py); this test is about the WIRE SHAPE, so the
    # policy is stubbed rather than exercised here.
    import vesper.execution_guard as eg
    monkeypatch.setitem(eg._MULTI_LEG_RISK_FORMULAS, "SINGLE_OPTION",
                        lambda legs: float(legs[0]["strike"]) * 100 * float(legs[0]["quantity"]))

    mock_wb = MagicMock()
    mock_wb.configured = True
    mock_wb.trade.account_v2.get_account_list.return_value = {
        "data": [{"account_id": "acct-1", "account_class": "INDIVIDUAL_CASH"}]
    }
    mock_wb.portfolio.return_value = {"totals": {"buying_power": 100000.0}}
    mock_wb.trade.order_v2.place_option.return_value = {"data": {"status": "WORKING"}}

    with patch("core.wb.Webull", return_value=mock_wb):
        res = await _execute_webull_multileg(_single_option_leg_proposal())

    assert res.status == "SUBMITTED"
    account_id, new_orders = mock_wb.trade.order_v2.place_option.call_args[0][:2]
    assert account_id == "acct-1"
    order = new_orders[0]

    # combo_type is an envelope value; the strategy lives in option_strategy.
    # Conflating them was the original bug.
    assert order["combo_type"] == "NORMAL"
    assert order["option_strategy"] == "SINGLE"
    assert order["side"] == "BUY", "side is required at the ORDER level too"

    leg = order["legs"][0]
    assert leg["side"] == "BUY", "...and again on the leg"
    assert leg["symbol"] == "SPY", "the UNDERLYING ticker, never a contract symbol"
    assert leg["strike_price"] == "770"
    assert leg["option_expire_date"] == "2026-12-18"
    assert leg["option_type"] == "CALL"
    assert leg["instrument_type"] == "OPTION"
    assert leg["market"] == "US"


@pytest.mark.asyncio
async def test_mixed_equity_and_option_combo_is_refused(monkeypatch):
    """Thega mixes 100 shares with option legs. place_option is an options
    endpoint and whether it accepts an equity leg is unverified -- refuse
    rather than send a guessed payload to a live order endpoint."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    with pytest.raises(GuardError, match="mixes EQUITY and OPTION"):
        await _execute_webull_multileg(_thega_proposal_for_refusal())


@pytest.mark.asyncio
async def test_multi_option_leg_combo_is_refused(monkeypatch):
    """SYNTHETIC_LONG is long call + short put. Only option_strategy=SINGLE is
    confirmed accepted; the correct enum for this combo is unknown, and
    guessing it on a live order is not worth the downside."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    prop = OrderProposal(
        id="prop-synth", ticker="NVDA", asset_type="OPTION", side="BUY",
        limit_price=8.50, quantity=1, strike=120.0, expiry="2026-12-18",
        option_type="call", strategy_type="SYNTHETIC_LONG",
        legs=[
            OrderLeg(side="BUY", asset_type="OPTION", option_type="call", strike=120.0,
                     expiry="2026-12-18", quantity=1, limit_price=8.50),
            OrderLeg(side="SELL", asset_type="OPTION", option_type="put", strike=120.0,
                     expiry="2026-12-18", quantity=1, limit_price=6.20),
        ],
        estimated_cost=12000.0, max_risk=12000.0,
    )
    with pytest.raises(GuardError, match="only.*single-leg option payload|option_strategy"):
        await _execute_webull_multileg(prop)


@pytest.mark.asyncio
async def test_option_leg_missing_contract_identifiers_is_refused(monkeypatch):
    """A leg is routed by underlying+strike+expiry+type. contract_symbol is NOT
    what Webull matches on and is no longer required -- but these are."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    prop = _single_option_leg_proposal()
    prop.legs[0].expiry = None
    with pytest.raises(GuardError, match="missing strike/expiry/option_type"):
        await _execute_webull_multileg(prop)


@pytest.mark.asyncio
async def test_single_option_leg_still_blocked_by_kill_switch():
    from vesper.execution_guard import TradingDisabled
    mock_wb = MagicMock(configured=True)
    mock_wb.trade.account_v2.get_account_list.return_value = {
        "data": [{"account_id": "acct-1", "account_class": "INDIVIDUAL_CASH"}]
    }
    mock_wb.portfolio.return_value = {"totals": {"buying_power": 100000.0}}
    with patch("core.wb.Webull", return_value=mock_wb):
        with pytest.raises(TradingDisabled):
            await _execute_webull_multileg(_single_option_leg_proposal())
    mock_wb.trade.order_v2.place_option.assert_not_called()
