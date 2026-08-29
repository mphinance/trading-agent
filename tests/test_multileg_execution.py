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
from vesper.paper_ledger import (
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
    monkeypatch.setattr("vesper.paper_ledger._DATA_DIR", data_dir)
    monkeypatch.setattr("vesper.paper_ledger._LEDGER_PATH", ledger_path)
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

def _synthetic_long_proposal_with_symbols() -> OrderProposal:
    return OrderProposal(
        id="prop-adxiv-synth-xyz789",
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
                     quantity=1, limit_price=8.50, contract_symbol="NVDA250919C00120000"),
            OrderLeg(side="SELL", option_type="put", strike=120.0, expiry="2025-09-19",
                     quantity=1, limit_price=6.20, contract_symbol="NVDA250919P00120000"),
        ],
        estimated_cost=12000.0,
        max_risk=12000.0,
    )


@pytest.mark.asyncio
async def test_execute_webull_multileg_refuses_leg_without_contract_symbol(monkeypatch):
    """Never fabricate a contract symbol -- refuse the whole combo instead."""
    prop = _synthetic_long_proposal_with_symbols()
    prop.legs[1].contract_symbol = None  # put leg lost its symbol

    with pytest.raises(GuardError, match="no contract_symbol"):
        await _execute_webull_multileg(prop)


@pytest.mark.asyncio
async def test_execute_webull_multileg_places_combo_order(monkeypatch):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "25000")
    prop = _synthetic_long_proposal_with_symbols()

    mock_wb = MagicMock()
    mock_wb.configured = True
    mock_wb.trade.account_v2.get_account_list.return_value = {
        "data": [{"account_id": "acct-1", "account_class": "INDIVIDUAL_CASH"}]
    }
    mock_wb.portfolio.return_value = {"totals": {"buying_power": 100000.0}}
    mock_wb.trade.order_v2.place_option.return_value = {"data": {"status": "WORKING"}}

    with patch("wb.Webull", return_value=mock_wb):
        result = await _execute_webull_multileg(prop)

    assert result.status == "SUBMITTED"
    mock_wb.trade.order_v2.place_option.assert_called_once()
    call_args = mock_wb.trade.order_v2.place_option.call_args
    account_id, new_orders = call_args[0][0], call_args[0][1]
    assert account_id == "acct-1"
    assert len(new_orders) == 1
    legs_sent = new_orders[0]["legs"]
    assert len(legs_sent) == 2
    symbols_sent = {leg["symbol"] for leg in legs_sent}
    assert symbols_sent == {"NVDA250919C00120000", "NVDA250919P00120000"}
    assert call_args[1]["client_combo_order_id"] == f"vesper-{prop.id}"


@pytest.mark.asyncio
async def test_execute_webull_multileg_blocked_by_kill_switch(monkeypatch):
    """VESPER_TRADING unset -> guard.preview raises before any broker call."""
    prop = _synthetic_long_proposal_with_symbols()

    mock_wb = MagicMock()
    mock_wb.configured = True
    mock_wb.trade.account_v2.get_account_list.return_value = {
        "data": [{"account_id": "acct-1", "account_class": "INDIVIDUAL_CASH"}]
    }
    mock_wb.portfolio.return_value = {"totals": {"buying_power": 100000.0}}

    from vesper.execution_guard import TradingDisabled
    with patch("wb.Webull", return_value=mock_wb):
        with pytest.raises(TradingDisabled):
            await _execute_webull_multileg(prop)

    mock_wb.trade.order_v2.place_option.assert_not_called()
