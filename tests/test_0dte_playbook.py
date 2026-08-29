"""Unit tests for 0DTE Flow Playbook live quote fetching and drafting."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from vesper.nodes.playbooks import (
    _fetch_0dte_option_quote,
    _select_0dte_delta_strike,
    _time_to_close_years,
    playbooks_node,
)
from vesper.state import MarketRegime, OptionAudit, OrderProposal, TechnicalAudit, TradingState


def _make_0dte_state(
    ticker: str = "SPY",
    spot: float = 560.0,
    flip: float = 550.0,
    iv: float = 0.85,
) -> TradingState:
    tech = TechnicalAudit(
        ticker=ticker,
        close=spot,
        rsi_14=55.0,
        rsi_state="NEUTRAL",
        ema_stack="BULLISH",
        atr_14=4.0,
        adx_14=22.0,
        summary=f"{ticker} 0DTE test",
    )
    regime = MarketRegime(
        posture="BULLISH",
        spy_spot=spot,
        spy_gamma_flip=flip,
    )
    options_audits = {}
    if iv is not None:
        options_audits[ticker] = OptionAudit(
            ticker=ticker, option_type="call", strike=spot, expiry="2026-08-29", dte=0, iv=iv,
        )
    return {
        "session_id": "test-0dte-sess",
        "selected_playbook": "0dte",
        "candidates": [ticker],
        "technicals": {ticker: tech},
        "options_audits": options_audits,
        "regime": regime,
        "proposals": [],
        "risk_assessments": {},
        "needs_human_approval": False,
        "audit_trail": [],
    }


def test_fetch_0dte_option_quote_picks_today_contract_only():
    """Verify that only a contract expiring today is matched and priced."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    future_str = "2029-12-31"

    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock()
        mock_wb.configured = True
        mock_wb_cls.return_value = mock_wb

        with patch("md.Market") as mock_mkt_cls:
            mock_mkt = MagicMock()
            mock_mkt.option_chain.return_value = [
                {
                    "symbol": "SPY_FUTURE_CALL",
                    "strike_price": 561.0,
                    "expire_date": future_str,
                },
                {
                    "symbol": "SPY_TODAY_CALL",
                    "strike_price": 561.0,
                    "expire_date": today_str,
                },
            ]
            mock_mkt.option_snapshot.return_value = {
                "SPY_TODAY_CALL": {"bid": 2.30, "ask": 2.40, "last": 2.35},
                "SPY_FUTURE_CALL": {"bid": 50.0, "ask": 52.0, "last": 51.0},
            }
            mock_mkt_cls.return_value = mock_mkt

            price = _fetch_0dte_option_quote("SPY", 561.0, "CALL")
            assert price == 2.35


def test_fetch_0dte_option_quote_returns_none_when_no_today_expiry():
    """Verify returns None when no contracts in the chain expire today (never fall back to later expiry)."""
    future_str = "2029-12-31"

    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock()
        mock_wb.configured = True
        mock_wb_cls.return_value = mock_wb

        with patch("md.Market") as mock_mkt_cls:
            mock_mkt = MagicMock()
            mock_mkt.option_chain.return_value = [
                {
                    "symbol": "SPY_FUTURE_CALL",
                    "strike_price": 561.0,
                    "expire_date": future_str,
                }
            ]
            mock_mkt_cls.return_value = mock_mkt

            price = _fetch_0dte_option_quote("SPY", 561.0, "CALL")
            assert price is None


def test_fetch_0dte_option_quote_unconfigured():
    """Verify returns None when Webull is unconfigured."""
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock()
        mock_wb.configured = False
        mock_wb_cls.return_value = mock_wb

        price = _fetch_0dte_option_quote("SPY", 561.0, "CALL")
        assert price is None


@pytest.mark.asyncio
async def test_0dte_playbook_drafts_proposal_with_live_quote():
    """Verify 0DTE flow drafts proposal with real quote and correct sizing,
    using a strike from a major OI wall (not spot+/-1)."""
    state = _make_0dte_state(ticker="SPY", spot=560.0, flip=550.0, iv=0.85)

    with patch("vesper.nodes.playbooks._select_0dte_wall_strike", return_value=563.0):
        with patch("vesper.nodes.playbooks._fetch_0dte_option_quote", return_value=2.45):
            res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    p: OrderProposal = props[0]
    assert p.ticker == "SPY"
    assert p.asset_type == "OPTION"
    assert p.side == "BUY"
    assert p.option_type == "call"
    assert p.strike == 563.0  # from the wall, not spot(560)+1
    assert p.quantity == 1
    assert p.limit_price == 2.45
    assert p.estimated_cost == 245.0
    assert res["needs_human_approval"] is True

    notes = res["audit_trail"][0]["notes"]
    assert any(
        "Drafted 0DTE SPY CALL Strike 563.0 @ $2.45" in n and "IV=85.0%" in n and "wall" in n
        for n in notes
    )


@pytest.mark.asyncio
async def test_0dte_playbook_skips_when_iv_below_70pct():
    """Requires IV >= 70% -- the tightening's headline requirement."""
    state = _make_0dte_state(ticker="SPY", spot=560.0, flip=550.0, iv=0.40)
    res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    notes = res["audit_trail"][0]["notes"]
    assert any("Skipped 0DTE SPY CALL: IV" in n and "40.0% < 70%" in n for n in notes)


@pytest.mark.asyncio
async def test_0dte_playbook_skips_when_iv_unavailable():
    """No fabricated 'assume it's high enough' when IV data is simply missing."""
    state = _make_0dte_state(ticker="SPY", spot=560.0, flip=550.0, iv=None)
    res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    notes = res["audit_trail"][0]["notes"]
    assert any("Skipped 0DTE SPY CALL: IV unavailable" in n for n in notes)


@pytest.mark.asyncio
async def test_0dte_playbook_skips_when_no_wall_strike_available():
    """No fabricated fallback to spot+/-1 when neither delta nor wall selection
    has a real strike to anchor to."""
    state = _make_0dte_state(ticker="SPY", spot=560.0, flip=550.0, iv=0.85)

    with patch("vesper.nodes.playbooks._select_0dte_delta_strike", return_value=None):
        with patch("vesper.nodes.playbooks._select_0dte_wall_strike", return_value=None):
            res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    notes = res["audit_trail"][0]["notes"]
    assert any(
        "Skipped 0DTE SPY CALL: no delta-based or wall-based strike available to anchor a strike to" in n
        for n in notes
    )


@pytest.mark.asyncio
async def test_0dte_playbook_skips_when_live_quote_unavailable():
    """Verify 0DTE flow skips drafting and logs audit note when live quote is unavailable
    (including a quote rejected for a too-wide bid/ask spread -- same skip path)."""
    state = _make_0dte_state(ticker="SPY", spot=560.0, flip=550.0, iv=0.85)

    with patch("vesper.nodes.playbooks._select_0dte_wall_strike", return_value=561.0):
        with patch("vesper.nodes.playbooks._fetch_0dte_option_quote", return_value=None):
            res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    assert res["needs_human_approval"] is False

    notes = res["audit_trail"][0]["notes"]
    assert any(
        "Skipped 0DTE SPY CALL strike $561.00: no live same-day option quote available" in n
        for n in notes
    )


# ── Wall-based strike selection ─────────────────────────────────────────────

def test_select_0dte_wall_strike_picks_highest_oi_on_the_bullish_side():
    from vesper.nodes.playbooks import _select_0dte_wall_strike

    with patch("td.TDPro") as mock_td_cls:
        mock_td = MagicMock()
        mock_td.configured = True
        mock_td.levels.return_value = {
            "walls": [
                {"strike": 555.0, "side": "below", "call_oi": 1000, "put_oi": 50000},
                {"strike": 562.0, "side": "above", "call_oi": 8000, "put_oi": 200},
                {"strike": 565.0, "side": "above", "call_oi": 30000, "put_oi": 100},
            ]
        }
        mock_td_cls.return_value = mock_td

        strike = _select_0dte_wall_strike("SPY", is_bullish=True)
        assert strike == 565.0  # highest call_oi on the "above" side


def test_select_0dte_wall_strike_returns_none_when_no_wall_on_side():
    from vesper.nodes.playbooks import _select_0dte_wall_strike

    with patch("td.TDPro") as mock_td_cls:
        mock_td = MagicMock()
        mock_td.configured = True
        mock_td.levels.return_value = {"walls": [{"strike": 555.0, "side": "below", "put_oi": 50000}]}
        mock_td_cls.return_value = mock_td

        strike = _select_0dte_wall_strike("SPY", is_bullish=True)  # wants "above", none exist
        assert strike is None


def test_select_0dte_wall_strike_returns_none_when_tdpro_unconfigured():
    from vesper.nodes.playbooks import _select_0dte_wall_strike

    with patch("td.TDPro") as mock_td_cls:
        mock_td_cls.return_value = MagicMock(configured=False)
        assert _select_0dte_wall_strike("SPY", is_bullish=True) is None


def test_select_0dte_wall_strike_returns_none_on_levels_error():
    from vesper.nodes.playbooks import _select_0dte_wall_strike

    with patch("td.TDPro") as mock_td_cls:
        mock_td = MagicMock()
        mock_td.configured = True
        mock_td.levels.return_value = {"error": "TD_API_KEY not set"}
        mock_td_cls.return_value = mock_td
        assert _select_0dte_wall_strike("SPY", is_bullish=True) is None


# ── Bid/ask spread rejection ─────────────────────────────────────────────────

def test_fetch_0dte_option_quote_rejects_wide_spread():
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock(configured=True)
        mock_wb_cls.return_value = mock_wb

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with patch("md.Market") as mock_mkt_cls:
            mock_mkt = MagicMock()
            mock_mkt.option_chain.return_value = [
                {"symbol": "SPY_TODAY_CALL", "strike_price": 561.0, "expire_date": today_str},
            ]
            # bid=1.00, ask=1.50: spread = 0.50/1.25 = 40%, well over the 15% max.
            # No "last" so the function is forced to fall through to bid/ask.
            mock_mkt.option_snapshot.return_value = {
                "SPY_TODAY_CALL": {"bid": 1.00, "ask": 1.50}
            }
            mock_mkt_cls.return_value = mock_mkt

            price = _fetch_0dte_option_quote("SPY", 561.0, "CALL")
            assert price is None


def test_fetch_0dte_option_quote_accepts_tight_spread():
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock(configured=True)
        mock_wb_cls.return_value = mock_wb

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with patch("md.Market") as mock_mkt_cls:
            mock_mkt = MagicMock()
            mock_mkt.option_chain.return_value = [
                {"symbol": "SPY_TODAY_CALL", "strike_price": 561.0, "expire_date": today_str},
            ]
            # bid=2.30, ask=2.40: spread ~4.3%, within the 15% max.
            mock_mkt.option_snapshot.return_value = {
                "SPY_TODAY_CALL": {"bid": 2.30, "ask": 2.40}
            }
            mock_mkt_cls.return_value = mock_mkt

            price = _fetch_0dte_option_quote("SPY", 561.0, "CALL")
            assert price == 2.35


# ── Delta-based (0.30-delta) strike selection ───────────────────────────────

def _mock_today_chain(strikes: list[float], option_type: str) -> list[dict]:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [
        {
            "symbol": f"SPY_{option_type}_{int(k)}",
            "strike_price": k,
            "expire_date": today_str,
        }
        for k in strikes
    ]


def test_select_0dte_delta_strike_picks_closest_to_030():
    """Real Black-Scholes math, not a mocked delta: S=560, sigma=0.85 (this
    repo's own test IV fixture), t=2h-to-close. Verified offline that strike
    564 (delta=0.2922) is closest to 0.30, distinct from the next-nearest
    strike 563 (delta=0.3414)."""
    strikes = [561.0, 562.0, 563.0, 564.0, 565.0, 566.0, 567.0, 568.0]

    with patch("vesper.nodes.playbooks._time_to_close_years", return_value=2 / 24 / 365):
        with patch("wb.Webull") as mock_wb_cls:
            mock_wb = MagicMock(configured=True)
            mock_wb_cls.return_value = mock_wb

            with patch("md.Market") as mock_mkt_cls:
                mock_mkt = MagicMock()
                mock_mkt.option_chain.return_value = _mock_today_chain(strikes, "CALL")
                mock_mkt_cls.return_value = mock_mkt

                strike = _select_0dte_delta_strike("SPY", spot=560.0, is_bullish=True, iv_pct=85.0)

    assert strike == 564.0


def test_select_0dte_delta_strike_put_side_uses_negative_delta():
    """Puts carry negative delta -- the selector must compare abs(delta),
    not delta itself. Verified offline: strike 556 (delta=-0.2859) is
    closest to 0.30, distinct from 557 (delta=-0.3353)."""
    strikes = [552.0, 553.0, 554.0, 555.0, 556.0, 557.0, 558.0, 559.0]

    with patch("vesper.nodes.playbooks._time_to_close_years", return_value=2 / 24 / 365):
        with patch("wb.Webull") as mock_wb_cls:
            mock_wb = MagicMock(configured=True)
            mock_wb_cls.return_value = mock_wb

            with patch("md.Market") as mock_mkt_cls:
                mock_mkt = MagicMock()
                mock_mkt.option_chain.return_value = _mock_today_chain(strikes, "PUT")
                mock_mkt_cls.return_value = mock_mkt

                strike = _select_0dte_delta_strike("SPY", spot=560.0, is_bullish=False, iv_pct=85.0)

    assert strike == 556.0


def test_select_0dte_delta_strike_returns_none_when_no_today_expiry():
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock(configured=True)
        mock_wb_cls.return_value = mock_wb

        with patch("md.Market") as mock_mkt_cls:
            mock_mkt = MagicMock()
            mock_mkt.option_chain.return_value = [
                {"symbol": "SPY_FUTURE_CALL", "strike_price": 565.0, "expire_date": "2029-12-31"},
            ]
            mock_mkt_cls.return_value = mock_mkt

            strike = _select_0dte_delta_strike("SPY", spot=560.0, is_bullish=True, iv_pct=85.0)

    assert strike is None


def test_select_0dte_delta_strike_returns_none_when_wb_unconfigured():
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb_cls.return_value = MagicMock(configured=False)
        strike = _select_0dte_delta_strike("SPY", spot=560.0, is_bullish=True, iv_pct=85.0)

    assert strike is None


def test_select_0dte_delta_strike_returns_none_when_missing_spot_or_iv():
    """Never compute Black-Scholes off missing/zero inputs."""
    assert _select_0dte_delta_strike("SPY", spot=0.0, is_bullish=True, iv_pct=85.0) is None
    assert _select_0dte_delta_strike("SPY", spot=560.0, is_bullish=True, iv_pct=0.0) is None
    assert _select_0dte_delta_strike("SPY", spot=560.0, is_bullish=True, iv_pct=None) is None


def test_select_0dte_delta_strike_returns_none_when_pyvollib_import_fails():
    """Proves the fallback path is reachable even if py_vollib itself breaks
    (library missing or broken -- a graceful skip, never a crash)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "py_vollib.black_scholes.greeks.analytical":
            raise ImportError("simulated py_vollib absence")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        strike = _select_0dte_delta_strike("SPY", spot=560.0, is_bullish=True, iv_pct=85.0)

    assert strike is None


def test_time_to_close_years_floors_near_market_close():
    """In the last minutes before 4pm ET, t must never hit (or go below) zero."""
    from datetime import datetime as dt, timezone as tz
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    almost_close = dt(2026, 8, 28, 15, 59, 58, tzinfo=ET).astimezone(tz.utc)
    years = _time_to_close_years(almost_close)
    assert years > 0
    from vesper.nodes.playbooks import MIN_0DTE_TTE_YEARS

    assert years == MIN_0DTE_TTE_YEARS


@pytest.mark.asyncio
async def test_0dte_playbook_prefers_delta_over_wall_when_both_available():
    """Delta-based selection is tried first; when it succeeds, it wins over
    the wall-based fallback even though both are available."""
    state = _make_0dte_state(ticker="SPY", spot=560.0, flip=550.0, iv=0.85)

    with patch("vesper.nodes.playbooks._select_0dte_delta_strike", return_value=564.0):
        with patch("vesper.nodes.playbooks._select_0dte_wall_strike", return_value=563.0):
            with patch("vesper.nodes.playbooks._fetch_0dte_option_quote", return_value=2.45):
                res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    assert props[0].strike == 564.0  # delta pick, not the wall's 563.0

    notes = res["audit_trail"][0]["notes"]
    assert any("Strike 564.0" in n and "0.30-delta" in n for n in notes)


@pytest.mark.asyncio
async def test_0dte_playbook_falls_back_to_wall_when_delta_selection_returns_none():
    """When delta selection can't produce a strike, the wall-based fallback
    still drafts -- this is the pre-existing end-to-end path, still exercised
    with an explicit patch here (real _select_0dte_delta_strike also returns
    None in this test env since wb.Webull().configured is False, but the
    explicit patch keeps the test's intent obvious and independent of that)."""
    state = _make_0dte_state(ticker="SPY", spot=560.0, flip=550.0, iv=0.85)

    with patch("vesper.nodes.playbooks._select_0dte_delta_strike", return_value=None):
        with patch("vesper.nodes.playbooks._select_0dte_wall_strike", return_value=563.0):
            with patch("vesper.nodes.playbooks._fetch_0dte_option_quote", return_value=2.45):
                res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    assert props[0].strike == 563.0

    notes = res["audit_trail"][0]["notes"]
    assert any(
        "Strike 563.0" in n and "delta selection unavailable" in n for n in notes
    )


@pytest.mark.asyncio
async def test_0dte_playbook_skips_when_both_selectors_fail():
    """Preserves the 'skip the draft' behavior when neither delta nor wall
    selection can anchor a strike."""
    state = _make_0dte_state(ticker="SPY", spot=560.0, flip=550.0, iv=0.85)

    with patch("vesper.nodes.playbooks._select_0dte_delta_strike", return_value=None):
        with patch("vesper.nodes.playbooks._select_0dte_wall_strike", return_value=None):
            res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    notes = res["audit_trail"][0]["notes"]
    assert any(
        "Skipped 0DTE SPY CALL: no delta-based or wall-based strike available to anchor a strike to" in n
        for n in notes
    )
