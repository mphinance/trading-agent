"""Unit tests for ADX / IV Option-Style Router Playbook."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from vesper.nodes.playbooks import playbooks_node, _fetch_leaps_option_quote, _fetch_synthetic_long_quotes
from vesper.state import TradingState, TechnicalAudit, OptionAudit, OrderProposal


def _make_state(
    ticker: str,
    close: float,
    adx_14: float,
    iv: float,
    selected_playbook: str = "adx_iv",
    ema_34: float = None,
    sma_200: float = None,
    keltner_lower: float = None,
) -> TradingState:
    tech = TechnicalAudit(
        ticker=ticker,
        close=close,
        rsi_14=50.0,
        rsi_state="NEUTRAL",
        ema_stack="NEUTRAL",
        atr_14=close * 0.03,
        adx_14=adx_14,
        ema_34=ema_34,
        sma_200=sma_200,
        keltner_lower=keltner_lower,
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

    # No swing-stop basis was available on the stub -> underlying_stop_type
    # must stay None (never fabricated), leaving only the flat contract stop.
    assert p.underlying_stop_type is None
    assert p.underlying_stop_basis is None


@pytest.mark.asyncio
async def test_adx_iv_router_branch3_leaps_sets_underlying_stop_basis_preferring_ema_34():
    """LEAPS branch: when ema_34 is available on the technical audit, it wins
    over sma_200/keltner_lower (first non-None in the ema_34 -> sma_200 ->
    keltner_lower preference order)."""
    state = _make_state(
        ticker="MSFT", close=440.0, adx_14=27.0, iv=0.28,
        ema_34=430.0, sma_200=400.0, keltner_lower=420.0,
    )

    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        with patch("vesper.nodes.playbooks._fetch_leaps_option_quote", return_value=(18.50, "2025-06-20")):
            res = await playbooks_node(state)

    p: OrderProposal = res["proposals"][0]
    assert p.underlying_stop_type == "underlying_level"
    assert p.underlying_stop_basis == "ema_34"


@pytest.mark.asyncio
async def test_adx_iv_router_branch3_leaps_falls_back_to_sma_200_then_keltner_lower():
    """When ema_34 is unavailable, falls back to sma_200; when both ema_34 and
    sma_200 are unavailable, falls back to keltner_lower."""
    state_sma = _make_state(
        ticker="MSFT", close=440.0, adx_14=27.0, iv=0.28,
        ema_34=None, sma_200=400.0, keltner_lower=420.0,
    )
    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        with patch("vesper.nodes.playbooks._fetch_leaps_option_quote", return_value=(18.50, "2025-06-20")):
            res_sma = await playbooks_node(state_sma)
    assert res_sma["proposals"][0].underlying_stop_basis == "sma_200"

    state_kc = _make_state(
        ticker="MSFT", close=440.0, adx_14=27.0, iv=0.28,
        ema_34=None, sma_200=None, keltner_lower=420.0,
    )
    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        with patch("vesper.nodes.playbooks._fetch_leaps_option_quote", return_value=(18.50, "2025-06-20")):
            res_kc = await playbooks_node(state_kc)
    assert res_kc["proposals"][0].underlying_stop_basis == "keltner_lower"


@pytest.mark.asyncio
async def test_adx_iv_router_branch4_synthetic_long_drafts_multileg_combo():
    """Branch 4: ADX >= 20 + IV >= 70% -> Synthetic Long (BUY call + SELL put,
    same strike/expiry). Now landed via OrderProposal.legs + execution_guard's
    SYNTHETIC_LONG risk formula -- see docs/... multi-leg design note."""
    state = _make_state(ticker="NVDA", close=120.0, adx_14=32.0, iv=0.88, ema_34=118.0)

    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        with patch(
            "vesper.nodes.playbooks._fetch_synthetic_long_quotes",
            return_value=(8.50, 6.20, "2025-09-19", "NVDA250919C00120000", "NVDA250919P00120000"),
        ):
            res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    p: OrderProposal = props[0]
    assert p.ticker == "NVDA"
    assert p.strategy_type == "SYNTHETIC_LONG"
    assert p.strike == 120.0
    assert p.expiry == "2025-09-19"
    assert res["needs_human_approval"] is True

    assert p.legs is not None
    assert len(p.legs) == 2
    call_leg = next(l for l in p.legs if l.option_type == "call")
    put_leg = next(l for l in p.legs if l.option_type == "put")
    assert call_leg.side == "BUY"
    assert call_leg.limit_price == 8.50
    assert put_leg.side == "SELL"
    assert put_leg.limit_price == 6.20
    assert call_leg.strike == put_leg.strike == 120.0
    assert call_leg.expiry == put_leg.expiry == "2025-09-19"
    assert call_leg.contract_symbol == "NVDA250919C00120000"
    assert put_leg.contract_symbol == "NVDA250919P00120000"

    # Assignment capital commitment = strike * 100 * 1 = $12,000 (the short
    # put leg is the capital-at-risk driver, same reasoning as the Wheel CSP)
    assert p.estimated_cost == 12000.0
    assert p.max_risk == 12000.0

    # Combo-level swing stop from the stubbed ema_34
    assert p.underlying_stop_type == "underlying_level"
    assert p.underlying_stop_basis == "ema_34"

    notes = res["audit_trail"][0]["notes"]
    assert any(
        "[Synthetic Long] for NVDA" in n and "Assignment Notional: $12,000.00" in n
        for n in notes
    )


@pytest.mark.asyncio
async def test_adx_iv_router_branch4_skips_when_no_shared_expiry_quote():
    """If call and put can't be quoted against the same expiry, skip rather
    than approximate with mismatched legs (execution_guard would reject a
    mismatched-expiry payload anyway, but playbooks shouldn't draft one)."""
    state = _make_state(ticker="NVDA", close=120.0, adx_14=32.0, iv=0.88)

    with patch("vesper.nodes.playbooks.fetch_live_equity", return_value=50000.0):
        with patch("vesper.nodes.playbooks._fetch_synthetic_long_quotes", return_value=None):
            res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    assert res["needs_human_approval"] is False
    notes = res["audit_trail"][0]["notes"]
    assert any(
        "Skipped ADX/IV Router [Synthetic Long] for NVDA" in n and "no shared-expiry" in n
        for n in notes
    )


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


def test_fetch_synthetic_long_quotes_picks_shared_expiry_only():
    """Verify it only returns an expiry that has BOTH a call and a put quoted
    at the strike -- a call-only or put-only expiry must be ignored even if
    it's the nearest-dated one for that single leg."""
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock()
        mock_wb.configured = True
        mock_wb_cls.return_value = mock_wb

        with patch("md.Market") as mock_mkt_cls:
            mock_mkt = MagicMock()

            def option_chain(underlying, option_type, strike_gte, strike_lte):
                if option_type == "CALL":
                    return [
                        # Nearer expiry, but no matching put below -- must be skipped.
                        {"symbol": "SPY_C_0901", "strike_price": 550.0, "expire_date": "2025-09-01"},
                        {"symbol": "SPY_C_0919", "strike_price": 550.0, "expire_date": "2025-09-19"},
                    ]
                return [
                    {"symbol": "SPY_P_0919", "strike_price": 550.0, "expire_date": "2025-09-19"},
                ]

            def option_snapshot(symbols):
                sym = symbols[0]
                prices = {
                    "SPY_C_0919": {"last": 12.0},
                    "SPY_P_0919": {"last": 9.0},
                }
                return {sym: prices[sym]}

            mock_mkt.option_chain.side_effect = option_chain
            mock_mkt.option_snapshot.side_effect = option_snapshot
            mock_mkt_cls.return_value = mock_mkt

            res = _fetch_synthetic_long_quotes("SPY", 550.0)
            assert res is not None
            call_premium, put_premium, expiry, call_sym, put_sym = res
            assert expiry == "2025-09-19"
            assert call_premium == 12.0
            assert put_premium == 9.0
            assert call_sym == "SPY_C_0919"
            assert put_sym == "SPY_P_0919"


def test_fetch_synthetic_long_quotes_none_when_no_shared_expiry():
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock()
        mock_wb.configured = True
        mock_wb_cls.return_value = mock_wb

        with patch("md.Market") as mock_mkt_cls:
            mock_mkt = MagicMock()

            def option_chain(underlying, option_type, strike_gte, strike_lte):
                if option_type == "CALL":
                    return [{"symbol": "SPY_C_0901", "strike_price": 550.0, "expire_date": "2025-09-01"}]
                return [{"symbol": "SPY_P_0919", "strike_price": 550.0, "expire_date": "2025-09-19"}]

            mock_mkt.option_chain.side_effect = option_chain
            mock_mkt_cls.return_value = mock_mkt

            res = _fetch_synthetic_long_quotes("SPY", 550.0)
            assert res is None
