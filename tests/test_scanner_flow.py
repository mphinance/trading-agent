"""Unit tests for scanner_node unusual options flow screening and classification."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from vesper.nodes.scanner import scanner_node
from vesper.state import MarketRegime, TradingState


@pytest.fixture
def empty_screeners():
    """Mock out VCP, momentum screener, and TickerTrace briefing so scanner tests isolate flow logic."""
    with patch("vesper.nodes.scanner.screen_vcp", return_value=MagicMock(data=[])):
        with patch("vesper.nodes.scanner.run_stock_screen", return_value=[]):
            with patch("vesper.nodes.scanner.get_briefing", return_value={}):
                yield


@pytest.mark.asyncio
async def test_scanner_node_promotes_directional_flow_candidate(empty_screeners):
    """Directional unusual activity prints are promoted to Candidate with source=UNUSUAL_FLOW."""
    mock_td = MagicMock()
    mock_td.configured = True
    mock_td.cached.return_value = {
        "data": [
            {
                "ticker": "AMD",
                "type": "CALL",
                "volume": 12000,
                "openInterest": 1500,  # 8x OI
                "sentiment": "Bullish",
                "sentimentLabel": "Bullish (Calls Opening)",
                "vsOI": 800.0,
                "score": 88,
                "moneynessPct": 0.025,
            }
        ]
    }

    state: TradingState = {
        "session_id": "test-scan-flow",
        "selected_playbook": "options_flow",
        "candidates": [],
        "audit_trail": [],
    }

    with patch("core.td.TDPro", return_value=mock_td):
        res = await scanner_node(state)

    discovered = [c.ticker for c in res["candidates"]]
    assert "AMD" in discovered

    cand = next(c for c in res["candidates"] if c.ticker == "AMD")
    assert cand.source == "UNUSUAL_FLOW"
    assert cand.score == 8.5
    assert "Directional Options Flow" in cand.rationale


@pytest.mark.asyncio
async def test_scanner_node_filters_out_hedge_flow(empty_screeners):
    """Hedge-like flow near the gamma flip with low IV is rejected from becoming a Candidate."""
    mock_td = MagicMock()
    mock_td.configured = True
    mock_td.cached.return_value = {
        "data": [
            {
                "ticker": "SPY",
                "type": "PUT",
                "volume": 3000,
                "openInterest": 2800,
                "sentiment": "Bearish",
                "vsOI": 107.1,
                "score": 72,
                "moneynessPct": 0.0,
                "iv": 0.14,
            }
        ]
    }

    # Spot = 560, Flip = 559 -> 0.17% distance (tight to flip -> dealer/institutional hedge)
    regime = MarketRegime(
        posture="BULLISH",
        spy_spot=560.0,
        spy_gamma_flip=559.0,
    )

    state: TradingState = {
        "session_id": "test-scan-hedge",
        "selected_playbook": "flow",
        "candidates": [],
        "regime": regime,
        "audit_trail": [],
    }

    with patch("core.td.TDPro", return_value=mock_td):
        res = await scanner_node(state)

    # SPY was filtered out of the unusual flow step
    notes = res["audit_trail"][0]["notes"]
    assert any("Filtered out hedge flow for SPY" in n for n in notes)


@pytest.mark.asyncio
async def test_scanner_node_handles_unconfigured_tdpro(empty_screeners):
    """When TDPro is unconfigured, scanner_node proceeds without errors."""
    mock_td = MagicMock()
    mock_td.configured = False

    state: TradingState = {
        "session_id": "test-scan-unconfigured",
        "selected_playbook": "flow",
        "candidates": [],
        "audit_trail": [],
    }

    with patch("core.td.TDPro", return_value=mock_td):
        res = await scanner_node(state)

    assert len(res["candidates"]) == 0
    assert len(res["audit_trail"]) == 1
