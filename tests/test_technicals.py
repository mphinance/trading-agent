"""Hermetic tests for core/technicals.py."""

from __future__ import annotations

from unittest.mock import patch
import pandas as pd
import pytest

from core.cache import clear_cache
from core.technicals import (
    _build_analysis,
    _extract_at,
    _extract_last,
    analyze_technicals,
)


@pytest.fixture(autouse=True)
def reset_cache():
    clear_cache()
    yield
    clear_cache()


class TestTechnicalsHelpers:
    def test_extract_at_and_last(self):
        s = pd.Series([10.123456, float("nan"), 20.987654])
        assert _extract_last(s) == 20.9877
        assert _extract_at(s, 0) == 10.1235
        assert _extract_at(s, 1) is None
        assert _extract_at(s, 5) is None
        assert _extract_at(None) is None
        assert _extract_at(pd.Series([])) is None

    def test_build_analysis(self):
        # Neutral
        text = _build_analysis("AAPL", 150.0, 50.0, 1.0, 0.5, 0.5)
        assert "neutral range" in text
        assert "bullish crossover" in text
        assert "expanding at 0.5000" in text

        # Overbought & bearish
        text_ob = _build_analysis("TSLA", 250.0, 75.0, 0.5, 1.0, -0.5)
        assert "overbought territory" in text_ob
        assert "bearish crossover" in text_ob
        assert "contracting at -0.5000" in text_ob

        # Oversold
        text_os = _build_analysis("NVDA", 100.0, 25.0, None, None, None)
        assert "oversold territory" in text_os


class TestAnalyzeTechnicals:
    @pytest.mark.asyncio
    async def test_insufficient_bars_returns_error(self):
        short_data = [
            {"date": f"2026-01-{i:02d}", "open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000}
            for i in range(1, 20)
        ]
        with patch("core.technicals.get_historical_data", return_value=short_data):
            res = await analyze_technicals("AAPL")
            assert res.status == "error"
            assert "Insufficient data" in res.error

    @pytest.mark.asyncio
    async def test_successful_analysis(self):
        # 60 synthetic bars with upward trend
        bars = []
        price = 100.0
        for i in range(60):
            price += 0.5
            bars.append({
                "date": f"2026-01-{(i%28)+1:02d}",
                "open": price - 0.2,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price,
                "volume": 1000000 + i * 10000,
            })

        with patch("core.technicals.get_historical_data", return_value=bars), \
             patch("core.technicals.get_live_price", return_value=price + 0.1):
            res = await analyze_technicals("SPY")
            assert res.status == "success"
            data = res.data
            assert data["ticker"] == "SPY"
            assert data["close"] == price + 0.1
            assert data["rsi_14"] is not None
            assert data["ema_8"] is not None
            assert data["ema_21"] is not None
            assert data["adx_14"] is not None
            assert data["atr_14"] is not None
            assert "analysis" in data
            # 60 bars < 100 bars, so data_notes should mention SMA(100) or SMA(200)
            assert "data_notes" in data

    @pytest.mark.asyncio
    async def test_exception_in_historical_data(self):
        with patch("core.technicals.get_historical_data", side_effect=RuntimeError("Data provider down")):
            res = await analyze_technicals("FAIL")
            assert res.status == "error"
            assert "Data provider down" in res.error
