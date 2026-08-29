"""Unit tests for Options Order Flow Classifier (Directional vs Hedge)."""

from __future__ import annotations

import pytest
from vesper.flow_classifier import classify_flow, classify_unusual_activity_record


def test_classify_flow_clear_directional():
    """Large volume relative to OI, far from gamma flip, elevated IV rank -> DIRECTIONAL."""
    result = classify_flow(
        trade_size=10_000,
        open_interest=2_000,   # 5x OI
        iv=0.55,
        iv_rank=75.0,
        distance_from_flip_pct=3.5,  # 3.5% above flip
        option_type="CALL",
        moneyness_pct=2.0,     # OTM call
        sentiment="Bullish",
    )
    assert result == "DIRECTIONAL"


def test_classify_flow_clear_hedge_near_flip():
    """Large print right at the gamma flip with subdued IV -> HEDGE (dealer/institutional rebalancing)."""
    result = classify_flow(
        trade_size=5_000,
        open_interest=4_000,
        iv=0.14,
        iv_rank=18.0,
        distance_from_flip_pct=0.2,   # Right on flip
        option_type="CALL",
        moneyness_pct=0.0,            # ATM
    )
    assert result == "HEDGE"


def test_classify_flow_atm_put_overlay_hedge():
    """ATM PUT print near gamma inflection with low IV -> HEDGE (portfolio downside overlay)."""
    result = classify_flow(
        trade_size=3_000,
        open_interest=2_500,
        iv=0.16,
        iv_rank=22.0,
        distance_from_flip_pct=0.4,
        option_type="PUT",
        moneyness_pct=-0.5,
    )
    assert result == "HEDGE"


def test_classify_flow_ambiguous_low_volume():
    """Small volume relative to existing Open Interest -> AMBIGUOUS (routine order)."""
    result = classify_flow(
        trade_size=100,
        open_interest=5_000,
        iv=0.30,
        iv_rank=45.0,
        distance_from_flip_pct=2.0,
    )
    assert result == "AMBIGUOUS"


def test_classify_flow_ambiguous_zero_or_negative_size():
    """Zero or negative size returns AMBIGUOUS."""
    assert classify_flow(0, 1000, 0.3) == "AMBIGUOUS"
    assert classify_flow(-50, 1000, 0.3) == "AMBIGUOUS"


def test_classify_unusual_activity_record_confirmed_traderdaddy_schema():
    """Verifies classification against real TraderDaddy get_unusual_activity field names."""
    # Real payload shape confirmed from TraderDaddy get_unusual_activity MCP output
    td_record = {
        "id": 6513102,
        "ticker": "SPY",
        "type": "CALL",
        "premium": 3372120,
        "volume": 12920,
        "openInterest": 2161,
        "sentiment": "Bullish",
        "score": 73,
        "moneynessPct": -0.0018,
        "moneynessBucket": "ATM",
    }

    # Spot is 560, flip is 540 -> distance = (560 - 540)/560 = +3.57% (far from flip)
    res = classify_unusual_activity_record(
        record=td_record,
        spot_price=560.0,
        gamma_flip=540.0,
        iv_rank=60.0,
    )
    assert res == "DIRECTIONAL"
