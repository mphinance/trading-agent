"""TDPro screener/gapper/bounce discovery in scanner_node.

The scanner's job is to bring tickers TO the user -- no watchlist to curate.
These pin that the discovery sources are wired, use the real field names
(confirmed live 2026-08-30: run_screener rows key on `symbol`,
get_bounce_signals rows key on `ticker` -- they differ), and degrade rather
than raise when TDPro is unavailable.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from vesper.nodes.scanner import scanner_node, SCREENER_MAX_PER_PRESET


@pytest.fixture
def quiet_other_sources():
    with patch("vesper.nodes.scanner.screen_vcp", return_value=MagicMock(data=[])), \
         patch("vesper.nodes.scanner.run_stock_screen", return_value=[]), \
         patch("vesper.nodes.scanner.get_briefing", return_value={}):
        yield


def _td(screener_rows=None, gappers=None, signals=None, unusual=None):
    m = MagicMock()
    m.configured = True
    def _cached(tool, args=None):
        if tool == "run_screener":
            return {"results": screener_rows or []}
        if tool == "get_premarket_gappers":
            return {"gappers": gappers or []}
        if tool == "get_bounce_signals":
            return {"signals": signals or []}
        if tool == "get_unusual_activity":
            return {"data": unusual or []}
        return {}
    m.cached.side_effect = _cached
    return m


def _state(playbook="all"):
    return {"selected_playbook": playbook, "candidates": [], "audit_trail": []}


@pytest.mark.asyncio
async def test_screener_rows_become_candidates(quiet_other_sources):
    td = _td(screener_rows=[{"symbol": "CNK", "adx": 22.4, "rsi": 49.1},
                            {"symbol": "EMR", "adx": 31.0, "rsi": 55.0}])
    with patch("core.td.TDPro", return_value=td):
        res = await scanner_node(_state("momentum_squeeze"))
    got = {c.ticker: c for c in res["candidates"] if c.source == "TDPRO_SCREENER"}
    assert "CNK" in got and "EMR" in got
    assert "ADX 22" in got["CNK"].rationale, "indicator context should reach the rationale"


@pytest.mark.asyncio
async def test_bounce_signals_use_ticker_key_not_symbol(quiet_other_sources):
    """Regression: run_screener keys rows on `symbol`, get_bounce_signals on
    `ticker`. Reusing one key for both silently yields zero candidates."""
    td = _td(signals=[{"ticker": "APLD", "signalType": "bounce_bottom"}])
    with patch("core.td.TDPro", return_value=td):
        res = await scanner_node(_state("all"))
    assert any(c.ticker == "APLD" and c.source == "TDPRO_BOUNCE" for c in res["candidates"])


@pytest.mark.asyncio
async def test_gapper_that_failed_its_own_pillar_check_is_skipped(quiet_other_sources):
    """The feed publishes its own verdict in `cleared`; a gap it rejected is
    noise, not a candidate."""
    td = _td(gappers=[{"symbol": "GOOD", "changePct": 12.1, "cleared": True},
                      {"symbol": "BAD", "changePct": 40.0, "cleared": False}])
    with patch("core.td.TDPro", return_value=td):
        res = await scanner_node(_state("all"))
    tickers = {c.ticker for c in res["candidates"]}
    assert "GOOD" in tickers
    assert "BAD" not in tickers


@pytest.mark.asyncio
async def test_results_are_capped_per_preset(quiet_other_sources):
    """analyst_node runs real technicals per candidate against a 2-req/2s
    bucket, so an unbounded screener would blow up the next node."""
    td = _td(screener_rows=[{"symbol": f"T{i}"} for i in range(50)])
    with patch("core.td.TDPro", return_value=td):
        res = await scanner_node(_state("collar_following"))
    n = len([c for c in res["candidates"] if c.source == "TDPRO_SCREENER"])
    assert n <= SCREENER_MAX_PER_PRESET


@pytest.mark.asyncio
async def test_unconfigured_tdpro_degrades_without_raising(quiet_other_sources):
    td = MagicMock(); td.configured = False
    with patch("core.td.TDPro", return_value=td):
        res = await scanner_node(_state("all"))
    assert not [c for c in res["candidates"] if c.source == "TDPRO_SCREENER"]


@pytest.mark.asyncio
async def test_a_raising_screener_does_not_kill_the_scan(quiet_other_sources):
    """One bad source must not cost every other source's candidates."""
    td = _td(signals=[{"ticker": "APLD", "signalType": "bounce_bottom"}])
    def _boom(tool, args=None):
        if tool == "run_screener":
            raise RuntimeError("TDPro 500")
        return {"signals": [{"ticker": "APLD", "signalType": "bounce_bottom"}]} if tool == "get_bounce_signals" else {}
    td.cached.side_effect = _boom
    with patch("core.td.TDPro", return_value=td):
        res = await scanner_node(_state("all"))
    assert any(c.ticker == "APLD" for c in res["candidates"]), "other sources must survive"


@pytest.mark.asyncio
async def test_duplicate_tickers_across_sources_are_not_double_added(quiet_other_sources):
    td = _td(screener_rows=[{"symbol": "AMAT"}], signals=[{"ticker": "AMAT", "signalType": "bounce_bottom"}])
    with patch("core.td.TDPro", return_value=td):
        res = await scanner_node(_state("all"))
    assert len([c for c in res["candidates"] if c.ticker == "AMAT"]) == 1
