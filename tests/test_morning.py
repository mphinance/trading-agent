"""Tests for Module 1: Pre-Market Battle-Plan & Fallback Labeling."""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
from vesper.morning import generate_morning_plan


@pytest.mark.asyncio
async def test_morning_plan_stale_fallback_labeling(monkeypatch):
    """Verify morning plan flags GEX levels as STALE when TraderDaddy is offline/unconfigured."""
    # Ensure TDPro reports unconfigured
    mock_td = MagicMock()
    mock_td.configured = False
    monkeypatch.setattr("td.TDPro", lambda: mock_td)

    plan = await generate_morning_plan()
    assert plan["gex"]["SPY"]["status"] == "STALE"
    assert plan["gex"]["QQQ"]["status"] == "STALE"
    assert "UNAVAILABLE" in plan["gex"]["SPY"]["bias"]
    assert "UNAVAILABLE" in plan["gex"]["QQQ"]["bias"]


@pytest.mark.asyncio
async def test_morning_plan_live_data_labeling(monkeypatch):
    """Verify morning plan flags GEX levels as LIVE when TraderDaddy levels are fetched."""
    mock_td = MagicMock()
    mock_td.configured = True
    mock_td.levels.side_effect = lambda ticker: {
        "spot": 590.25 if ticker == "SPY" else 505.10,
        "gamma_flip": 588.00 if ticker == "SPY" else 502.00,
        "regime": "Positive Gamma",
    }
    mock_td.get_market_health.return_value = {"score": {"value": 5.5, "label": "HEALTHY"}}
    monkeypatch.setattr("td.TDPro", lambda: mock_td)

    plan = await generate_morning_plan()
    assert plan["gex"]["SPY"]["status"] == "LIVE"
    assert plan["gex"]["QQQ"]["status"] == "LIVE"
    assert plan["gex"]["SPY"]["spot"] == 590.25
    assert "BULLISH" in plan["gex"]["SPY"]["bias"]
