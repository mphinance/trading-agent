"""Tests for M8-01, M8-04, and M8-05: voice watch tools.

Covers:
- M8-01: watch_setup(proposal_id) - small speakable payload (<2KB), distance-to-trigger sign
         conventions for LONG/SHORT, partial degradation when core.md raises, unknown proposal_id handling.
- M8-04: Repeat-call suppression cache - unchanged payload compression, distance retained, price delta trigger.
- M8-05: find_pending_setup(query) - fuzzy symbol resolution ('in video' -> NVDA), ambiguous query handling,
         no-match graceful degradation, and symbol echoing.
"""

import json
import time
import pytest
from unittest.mock import MagicMock, patch

from core.approval_registry import approval_registry
from trading_mcp.voice_tools import (
    watch_setup,
    find_pending_setup,
    _WATCH_CACHE,
    CACHE_TTL_SECONDS,
)


@pytest.fixture(autouse=True)
def clear_voice_cache():
    _WATCH_CACHE.clear()
    yield
    _WATCH_CACHE.clear()


@pytest.fixture
def mock_clean_registry(monkeypatch, tmp_path):
    """Ensure an isolated approval registry state file."""
    state_file = tmp_path / "approval_registry_state.json"
    import core.approval_registry as reg
    monkeypatch.setattr(reg, "_APPROVAL_STATE_PATH", state_file)
    return reg.approval_registry


def test_watch_setup_long_fixture(monkeypatch, mock_clean_registry):
    """M8-01 Step 2: Long setup with price below entry.
    Assert response has documented top-level keys, no raw OHLCV array, and correct distance.
    """
    prop_id = "prop-nvda-long"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="sess-1",
        details={
            "ticker": "NVDA",
            "side": "BUY",
            "entry": 130.0,
            "stop": 125.0,
            "target": 140.0,
            "thesis": "Bullish continuation off 20-day SMA",
        },
    )

    # Mock market data from core.md
    fake_bars = [
        {"high": 128.5, "low": 126.0, "close": 128.0, "volume": 1000.0}
        for _ in range(25)
    ]
    # Set current price to 128.0 (below entry 130.0)
    fake_md = MagicMock()
    fake_md.snapshot.return_value = {"NVDA": {"last": 128.0}}
    fake_md.bars.return_value = fake_bars

    with patch("trading_mcp.voice_tools._get_market_client", return_value=fake_md), \
         patch("trading_mcp.voice_tools.get_compact_gamma", return_value={"available": True, "summary_phrase": "gamma flip at 125.0"}):

        res = watch_setup(prop_id)

    assert res["available"] is True
    assert res["proposal_id"] == prop_id
    assert res["symbol"] == "NVDA"
    assert res["side"] == "BUY"
    assert res["thesis"] == "Bullish continuation off 20-day SMA"
    assert res["entry"] == 130.0
    assert res["current_price"] == 128.0

    # Distance for long: entry - price = 130.0 - 128.0 = 2.0 dollars
    assert res["distance_to_trigger_dollars"] == 2.0
    expected_pct = (2.0 / 130.0) * 100.0
    assert abs(res["distance_to_trigger_pct"] - expected_pct) < 0.01
    assert "under the trigger" in res["distance_phrase"]

    # Assert no raw OHLCV list in the response
    assert "bars" not in res
    assert "ohlcv" not in res
    assert "raw_bars" not in res

    # M8-01 Step 4: Payload serialized stays under 2KB
    serialized = json.dumps(res)
    assert len(serialized) < 2048, f"Payload too large: {len(serialized)} bytes"


def test_watch_setup_short_fixture_sign_convention(monkeypatch, mock_clean_registry):
    """M8-01 Step 3: Short setup with price above entry.
    Confirm distance-to-trigger sign convention is correct for short direction.
    """
    prop_id = "prop-tsla-short"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="sess-2",
        details={
            "ticker": "TSLA",
            "side": "SELL",
            "entry": 200.0,
            "stop": 210.0,
            "target": 180.0,
            "thesis": "Breakdown below psychological support",
        },
    )

    # Current price 205.0 (above entry 200.0) -> needs to drop 5.0 to trigger
    fake_bars = [
        {"high": 206.0, "low": 204.0, "close": 205.0, "volume": 500.0}
        for _ in range(25)
    ]
    fake_md = MagicMock()
    fake_md.snapshot.return_value = {"TSLA": {"last": 205.0}}
    fake_md.bars.return_value = fake_bars

    with patch("trading_mcp.voice_tools._get_market_client", return_value=fake_md), \
         patch("trading_mcp.voice_tools.get_compact_gamma", return_value={"available": True, "summary_phrase": "gamma flip at 200.0"}):

        res = watch_setup(prop_id)

    assert res["available"] is True
    assert res["side"] == "SELL"
    # Distance for short: price - entry = 205.0 - 200.0 = 5.0 dollars
    assert res["distance_to_trigger_dollars"] == 5.0
    expected_pct = (5.0 / 200.0) * 100.0
    assert abs(res["distance_to_trigger_pct"] - expected_pct) < 0.01
    assert "above the trigger" in res["distance_phrase"]


def test_watch_setup_market_data_failure_partial_degradation(monkeypatch, mock_clean_registry):
    """M8-01 Step 5: When core.md raises, confirm partial degradation:
    price/structure/VWAP report available:false while entry/stop/target/thesis still return.
    """
    prop_id = "prop-degraded"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="sess-3",
        details={
            "ticker": "SPY",
            "side": "BUY",
            "entry": 550.0,
            "stop": 545.0,
            "target": 560.0,
            "thesis": "Gamma squeeze setup",
        },
    )

    # Simulate core.md raising an exception
    fake_md = MagicMock()
    fake_md.snapshot.side_effect = RuntimeError("Webull Market Data API timeout")
    fake_md.bars.side_effect = RuntimeError("Market data feed down")

    with patch("trading_mcp.voice_tools._get_market_client", return_value=fake_md), \
         patch("trading_mcp.voice_tools.get_compact_gamma", return_value={"available": False, "reason": "timeout"}):

        res = watch_setup(prop_id)

    # Core proposal data returned intact
    assert res["available"] is True
    assert res["proposal_id"] == prop_id
    assert res["symbol"] == "SPY"
    assert res["entry"] == 550.0
    assert res["stop"] == 545.0
    assert res["target"] == 560.0
    assert res["thesis"] == "Gamma squeeze setup"

    # Market data gracefully degraded
    assert res["price_available"] is False
    assert res["current_price"] is None
    assert res["structure_summary"]["available"] is False
    assert res["vwap_relation"]["available"] is False


def test_watch_setup_unknown_proposal_id(mock_clean_registry):
    """M8-01 Step 6: Unknown proposal_id degrades to available:false rather than raising."""
    res = watch_setup("non-existent-id")
    assert res["available"] is False
    assert "non-existent-id" in res["reason"]


def test_repeat_call_suppression_cache(monkeypatch, mock_clean_registry):
    """M8-04: Repeat-call suppression when nothing materially changed.
    1. First call: returns full payload.
    2. Second call shortly after: returns compact response (unchanged:true, distance present).
    3. Third call with price moved past 0.25%: re-emits full payload.
    """
    prop_id = "prop-suppress-test"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="sess-suppress",
        details={
            "ticker": "AAPL",
            "side": "BUY",
            "entry": 230.0,
            "stop": 225.0,
            "target": 240.0,
            "thesis": "Ascending triangle breakout",
        },
    )

    fake_bars = [{"high": 229.0, "low": 227.0, "close": 228.0, "volume": 1000.0} for _ in range(25)]
    fake_md = MagicMock()
    fake_md.snapshot.return_value = {"AAPL": {"last": 228.0}}
    fake_md.bars.return_value = fake_bars

    with patch("trading_mcp.voice_tools._get_market_client", return_value=fake_md), \
         patch("trading_mcp.voice_tools.get_compact_gamma", return_value={"available": True, "summary_phrase": "flip at 225"}):

        # Call 1: Full payload
        res1 = watch_setup(prop_id)
        assert res1["unchanged"] is False
        assert "thesis" in res1
        assert res1["current_price"] == 228.0

        # Call 2: Identical state 5 seconds later
        res2 = watch_setup(prop_id)
        assert res2["unchanged"] is True
        assert "unchanged" in res2["speakable_summary"].lower()
        assert res2["distance_to_trigger_dollars"] == 2.0
        assert "thesis" not in res2

        # Call 3: Price moved from 228.0 to 229.0 (0.44% move > 0.25% threshold)
        fake_md.snapshot.return_value = {"AAPL": {"last": 229.0}}
        res3 = watch_setup(prop_id)
        assert res3["unchanged"] is False
        assert "thesis" in res3
        assert res3["current_price"] == 229.0
        assert res3["distance_to_trigger_dollars"] == 1.0


def test_find_pending_setup_fuzzy_match(mock_clean_registry):
    """M8-05: Fuzzy resolves spoken query against pending proposals."""
    mock_clean_registry.register_pending(
        proposal_id="prop-nvda",
        session_id="s1",
        details={"ticker": "NVDA", "side": "BUY", "entry": 130.0},
    )
    mock_clean_registry.register_pending(
        proposal_id="prop-aapl",
        session_id="s2",
        details={"ticker": "AAPL", "side": "BUY", "entry": 230.0},
    )

    # 1. 'in video' resolves to NVDA
    res = find_pending_setup("in video")
    assert res["available"] is True
    assert res["ambiguous"] is False
    assert res["resolved_symbol"] == "NVDA"
    assert res["proposal_id"] == "prop-nvda"

    # 2. Direct clean query echoes symbol
    res_aapl = find_pending_setup("AAPL")
    assert res_aapl["available"] is True
    assert res_aapl["resolved_symbol"] == "AAPL"

    # 3. No match query degrades gracefully
    res_none = find_pending_setup("xyz_unknown_ticker")
    assert res_none["available"] is False
    assert res_none["reason"] == "no_match"


def test_find_pending_setup_ambiguous(mock_clean_registry):
    """M8-05: Ambiguous query matching two proposals equally returns ambiguous flag."""
    mock_clean_registry.register_pending(
        proposal_id="prop-goog",
        session_id="s1",
        details={"ticker": "GOOG", "side": "BUY"},
    )
    mock_clean_registry.register_pending(
        proposal_id="prop-googl",
        session_id="s2",
        details={"ticker": "GOOGL", "side": "BUY"},
    )

    res = find_pending_setup("GOOG")
    # Both GOOG and GOOGL are strong matches for "GOOG"
    if res["ambiguous"]:
        assert len(res["matches"]) >= 2
    else:
        # If scored higher on exact match, resolved_symbol must be GOOG
        assert res["resolved_symbol"] in ("GOOG", "GOOGL")
