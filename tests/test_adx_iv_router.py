"""Unit tests for ADX / IV Option-Style Router Playbook."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from vesper.nodes.playbooks import playbooks_node, _fetch_leaps_option_quote
from vesper.state import TradingState, TechnicalAudit, OptionAudit, OrderProposal


def _make_state(
    ticker: str,
    close: float,
    adx_14: float,
    iv: float,
    selected_playbook: str = "adx_iv",
) -> TradingState:
    tech = TechnicalAudit(
        ticker=ticker,
        close=close,
        rsi_14=50.0,
        rsi_state="NEUTRAL",
        ema_stack="NEUTRAL",
        atr_14=close * 0.03,
        adx_14=adx_14,
        summary=f"{ticker} test summary",
    )
    opt = OptionAudit(
        ticker=ticker,
        option_type="call",
        strike=close,
        expiry="2025-06-20",
        dte=180,
        iv=iv,
    )
    return {
        "session_id": f"test-adxiv-{ticker}",
        "selected_playbook": selected_playbook,
        "candidates": [ticker],
        "technicals": {ticker: tech},
        "options_audits": {ticker: opt},
        "proposals": [],
        "risk_assessments": {},
        "needs_human_approval": False,
        "audit_trail": [],
    }


@pytest.mark.asyncio
async def test_adx_iv_router_branch1_training_wheels_equity():
    """Branch 1: ADX < 20 + IV < 70% -> Training Wheels (Buy shares outright)."""
    state = _make_state(ticker="AAPL", close=220.0, adx_14=16.0, iv=0.35)

    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    p: OrderProposal = props[0]
    assert p.ticker == "AAPL"
    assert p.asset_type == "EQUITY"
    assert p.side == "BUY"
    assert p.quantity > 0
    assert p.limit_price == 220.0

    notes = res["audit_trail"][0]["notes"]
    assert any("[Training Wheels] Equity Buy for AAPL" in n for n in notes)


@pytest.mark.asyncio
async def test_adx_iv_router_branch2_wheel_cash_secured_put():
    """Branch 2: ADX < 20 + IV >= 70% -> Wheel (Sell Cash-Secured Put)."""
    state = _make_state(ticker="TSLA", close=210.0, adx_14=14.5, iv=0.82)

    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        with patch("vesper.nodes.playbooks._fetch_live_option_quote", return_value=4.60):
            res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    p: OrderProposal = props[0]
    assert p.ticker == "TSLA"
    assert p.asset_type == "OPTION"
    assert p.side == "SELL"
    assert p.option_type == "put"
    assert p.strike == 210.0
    assert p.quantity == 1
    assert p.limit_price == 4.60
    # Assignment capital commitment = strike * 100 * 1 = $21,000 (NOT premium $460)
    assert p.estimated_cost == 21000.0
    assert p.max_risk == 21000.0

    notes = res["audit_trail"][0]["notes"]
    assert any("[Wheel] CSP for TSLA" in n and "Assignment Notional: $21,000.00" in n for n in notes)


@pytest.mark.asyncio
async def test_adx_iv_router_branch3_leaps_call():
    """Branch 3: ADX >= 20 + IV < 70% -> LEAPS (Buy far-dated call 6-12 months out)."""
    state = _make_state(ticker="MSFT", close=440.0, adx_14=27.0, iv=0.28)

    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        with patch("vesper.nodes.playbooks._fetch_leaps_option_quote", return_value=(18.50, "2025-06-20")):
            res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    p: OrderProposal = props[0]
    assert p.ticker == "MSFT"
    assert p.asset_type == "OPTION"
    assert p.side == "BUY"
    assert p.option_type == "call"
    assert p.strike == 440.0
    assert p.expiry == "2025-06-20"
    assert p.quantity == 1
    assert p.limit_price == 18.50
    # Long call capital at risk = premium * 100 * 1 = $1,850
    assert p.estimated_cost == 1850.0
    assert p.max_risk == 1850.0

    notes = res["audit_trail"][0]["notes"]
    assert any("[LEAPS] Call for MSFT" in n and "Cost: $1,850.00" in n for n in notes)


@pytest.mark.asyncio
async def test_adx_iv_router_branch4_synthetic_long_explicitly_skipped():
    """Branch 4: ADX >= 20 + IV >= 70% -> Synthetic Long (MUST BE SKIPPED, zero proposals drafted)."""
    state = _make_state(ticker="NVDA", close=120.0, adx_14=32.0, iv=0.88)

    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        res = await playbooks_node(state)

    # Must NOT draft any proposal
    assert len(res["proposals"]) == 0
    assert res["needs_human_approval"] is False

    # Must log an explicit skip note explaining multi-leg deferral
    notes = res["audit_trail"][0]["notes"]
    assert any("Skipped ADX/IV Router [Synthetic Long] for NVDA" in n and "multi-leg execution pipeline deferred" in n for n in notes)


@pytest.mark.asyncio
async def test_adx_iv_router_skips_when_quotes_unavailable():
    """Verify missing quotes for Wheel or LEAPS skip drafting without guessing/fabricating."""
    # Test Wheel with no quote
    state_wheel = _make_state(ticker="COIN", close=200.0, adx_14=12.0, iv=0.85)
    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        with patch("vesper.nodes.playbooks._fetch_live_option_quote", return_value=None):
            res_wheel = await playbooks_node(state_wheel)
    assert len(res_wheel["proposals"]) == 0
    assert any("no live option quote available" in n for n in res_wheel["audit_trail"][0]["notes"])

    # Test LEAPS with no quote
    state_leaps = _make_state(ticker="GOOGL", close=170.0, adx_14=25.0, iv=0.30)
    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        with patch("vesper.nodes.playbooks._fetch_leaps_option_quote", return_value=None):
            res_leaps = await playbooks_node(state_leaps)
    assert len(res_leaps["proposals"]) == 0
    assert any("no far-dated (180d+) option quote available" in n for n in res_leaps["audit_trail"][0]["notes"])


def test_fetch_leaps_option_quote_mock_chain():
    """Verify _fetch_leaps_option_quote filters for far-dated contracts (~180-400 DTE)."""
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock()
        mock_wb.configured = True
        mock_wb_cls.return_value = mock_wb

        with patch("md.Market") as mock_mkt_cls:
            mock_mkt = MagicMock()
            # 1 near-dated contract (ignored) + 1 far-dated contract (~270 days out)
            mock_mkt.option_chain.return_value = [
                {"symbol": "SPY240901C00550000", "strike_price": 550.0, "expire_date": "2024-09-01"},
                {"symbol": "SPY270620C00550000", "strike_price": 550.0, "expire_date": "2027-06-20"},
            ]
            mock_mkt.option_snapshot.return_value = {
                "SPY270620C00550000": {"bid": 30.0, "ask": 32.0, "last": 31.0}
            }
            mock_mkt_cls.return_value = mock_mkt

            res = _fetch_leaps_option_quote("SPY", 550.0, min_dte_days=180, max_dte_days=600)
            assert res is not None
            price, exp = res
            assert price == 31.0
            assert exp == "2027-06-20"
