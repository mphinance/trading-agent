"""Hermetic tests for core/screener.py."""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from core.cache import clear_cache
from core.screener import (
    PRESET_FILTERS,
    _clean_value,
    _safe_float,
    _safe_int,
    run_custom_screen,
    run_stock_screen,
)


@pytest.fixture(autouse=True)
def reset_cache():
    clear_cache()
    yield
    clear_cache()


class TestScreenerSanitization:
    def test_safe_float(self):
        assert _safe_float(12.34567) == 12.3457
        assert _safe_float("42.5") == 42.5
        assert _safe_float(float("nan")) is None
        assert _safe_float(float("inf")) is None
        assert _safe_float(float("-inf")) is None
        assert _safe_float("invalid") is None
        assert _safe_float(None) is None

    def test_safe_int(self):
        assert _safe_int(10) == 10
        assert _safe_int("15") == 15
        assert _safe_int("not_an_int") is None
        assert _safe_int(None) is None

    def test_clean_value(self):
        assert _clean_value(None) is None
        assert _clean_value(float("nan")) is None
        assert _clean_value(float("inf")) is None
        assert _clean_value(10.5) == 10.5
        assert _clean_value("string_val") == "string_val"


class TestPresetFilters:
    def test_preset_filters_keys(self):
        expected_presets = [
            "most_active", "new_highs", "new_lows", "overbought", "oversold",
            "high_relative_volume", "gap_up", "gap_down", "bullish_ema_stack",
            "bearish_ema_stack", "high_momentum", "large_cap_undervalued",
            "top_gainers", "biggest_losers", "most_volatile",
            "pre_market_gainers", "pre_market_losers", "pre_market_active",
            "pre_market_gappers", "after_hours_gainers", "after_hours_losers",
            "after_hours_active",
        ]
        for p in expected_presets:
            assert p in PRESET_FILTERS
            assert callable(PRESET_FILTERS[p])


class TestRunStockScreen:
    @pytest.mark.asyncio
    async def test_unknown_preset_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown preset 'bogus_preset'"):
            await run_stock_screen(preset="bogus_preset")

    @pytest.mark.asyncio
    async def test_run_stock_screen_mocked(self):
        fake_df = pd.DataFrame([
            {
                "name": "NASDAQ:AAPL",
                "close": 150.25,
                "volume": 50000000,
                "RSI": 55.4,
                "EMA20": 148.0,
            },
            {
                "name": "NYSE:TSLA",
                "close": 200.5,
                "volume": 30000000,
                "RSI": float("nan"),
                "EMA20": 195.0,
            },
        ])

        with patch("tradingview_screener.Query.get_scanner_data", return_value=(2, fake_df)):
            # Force cache bypass if needed or run directly
            results = await run_stock_screen(preset="most_active", limit=10)

            assert len(results) == 2
            assert results[0]["ticker"] == "AAPL"
            assert results[0]["close"] == 150.25
            assert results[0]["volume"] == 50000000.0
            assert results[0]["RSI"] == 55.4

            assert results[1]["ticker"] == "TSLA"
            assert results[1]["close"] == 200.5
            assert "RSI" not in results[1]  # cleaned NaN was dropped

    @pytest.mark.asyncio
    async def test_run_stock_screen_empty_dataframe(self):
        with patch("tradingview_screener.Query.get_scanner_data", return_value=(0, pd.DataFrame())):
            results = await run_stock_screen(preset="new_highs", limit=5)
            assert results == []

    @pytest.mark.asyncio
    async def test_run_stock_screen_exception_returns_empty(self):
        with patch("tradingview_screener.Query.get_scanner_data", side_effect=RuntimeError("Network error")):
            results = await run_stock_screen(preset="top_gainers", limit=5)
            assert results == []


class TestRunCustomScreen:
    @pytest.mark.asyncio
    async def test_run_custom_screen_filters(self):
        fake_df = pd.DataFrame([
            {
                "name": "AMEX:SPY",
                "close": 500.0,
                "volume": 80000000,
                "RSI": 45.0,
            }
        ])

        filters = [
            {"field": "RSI", "operator": "<", "value": 50},
            {"field": "EMA20", "operator": ">", "value": "EMA50"},
            {"field": "", "operator": ">", "value": 10},  # malformed filter should be skipped
        ]

        with patch("tradingview_screener.Query.get_scanner_data", return_value=(1, fake_df)):
            results = await run_custom_screen(filters=filters, limit=10)
            assert len(results) == 1
            assert results[0]["ticker"] == "SPY"
            assert results[0]["close"] == 500.0

    @pytest.mark.asyncio
    async def test_run_custom_screen_empty(self):
        with patch("tradingview_screener.Query.get_scanner_data", return_value=(0, None)):
            results = await run_custom_screen(filters=[{"field": "RSI", "operator": ">", "value": 70}])
            assert results == []
