"""Unit tests for the Thega delta-neutral volatility harvest playbook
(100 shares + 1 ATM covered call + 3 ATM CSPs)."""

from __future__ import annotations

from unittest.mock import patch
import pytest

from vesper.nodes.playbooks import playbooks_node
from vesper.state import OptionAudit, OrderProposal, TechnicalAudit, TradingState


def _make_state(ticker: str = "GME", close: float = 50.0, iv: float = 0.85) -> TradingState:
    tech = TechnicalAudit(
        ticker=ticker, close=close, rsi_14=50.0, rsi_state="NEUTRAL",
        ema_stack="NEUTRAL", atr_14=close * 0.05, adx_14=20.0,
        summary=f"{ticker} thega test",
    )
    opt = OptionAudit(ticker=ticker, option_type="call", strike=close, expiry="2025-06-20", dte=30, iv=iv)
    return {
        "session_id": "test-thega-sess",
        "selected_playbook": "thega",
        "candidates": [ticker],
        "technicals": {ticker: tech},
        "options_audits": {ticker: opt},
        "proposals": [],
        "risk_assessments": {},
        "needs_human_approval": False,
        "audit_trail": [],
    }


@pytest.mark.asyncio
async def test_thega_drafts_100_shares_plus_covered_call_plus_3_csps():
    state = _make_state(ticker="GME", close=50.0, iv=0.85)

    with patch(
        "vesper.nodes.playbooks._fetch_synthetic_long_quotes",
        return_value=(1.50, 1.20, "2025-09-19", "GME250919C00050000", "GME250919P00050000"),
    ):
        with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=50.25):
            res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    p: OrderProposal = props[0]
    assert p.ticker == "GME"
    assert p.strategy_type == "THEGA"
    assert p.strike == 50.0
    assert p.expiry == "2025-09-19"
    assert res["needs_human_approval"] is True

    assert p.legs is not None and len(p.legs) == 3
    equity_leg = next(l for l in p.legs if l.asset_type == "EQUITY")
    call_leg = next(l for l in p.legs if l.option_type == "call")
    put_leg = next(l for l in p.legs if l.option_type == "put")

    assert equity_leg.side == "BUY"
    assert equity_leg.quantity == 100
    assert equity_leg.limit_price == 50.25

    assert call_leg.side == "SELL"
    assert call_leg.quantity == 1
    assert call_leg.strike == 50.0
    assert call_leg.contract_symbol == "GME250919C00050000"

    assert put_leg.side == "SELL"
    assert put_leg.quantity == 3
    assert put_leg.strike == 50.0
    assert put_leg.contract_symbol == "GME250919P00050000"

    # Max risk = equity notional (100*50.25=5025) + put assignment (50*100*3=15000)
    assert p.max_risk == pytest.approx(20025.0)
    assert p.estimated_cost == pytest.approx(20025.0)

    notes = res["audit_trail"][0]["notes"]
    assert any("Drafted Thega for GME" in n and "IV=85.0%" in n for n in notes)


@pytest.mark.asyncio
async def test_thega_skipped_when_iv_below_threshold():
    state = _make_state(ticker="GME", close=50.0, iv=0.40)  # below 70%
    res = await playbooks_node(state)
    assert len(res["proposals"]) == 0


@pytest.mark.asyncio
async def test_thega_skipped_when_no_shared_expiry_quote():
    state = _make_state(ticker="GME", close=50.0, iv=0.85)
    with patch("vesper.nodes.playbooks._fetch_synthetic_long_quotes", return_value=None):
        res = await playbooks_node(state)
    assert len(res["proposals"]) == 0
    notes = res["audit_trail"][0]["notes"]
    assert any("Skipped Thega for GME" in n and "no shared-expiry" in n for n in notes)


@pytest.mark.asyncio
async def test_thega_skipped_when_no_live_equity_quote():
    state = _make_state(ticker="GME", close=50.0, iv=0.85)
    with patch(
        "vesper.nodes.playbooks._fetch_synthetic_long_quotes",
        return_value=(1.50, 1.20, "2025-09-19", "GME250919C00050000", "GME250919P00050000"),
    ):
        with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=None):
            res = await playbooks_node(state)
    assert len(res["proposals"]) == 0
    notes = res["audit_trail"][0]["notes"]
    assert any("Skipped Thega for GME" in n and "no live equity quote" in n for n in notes)
