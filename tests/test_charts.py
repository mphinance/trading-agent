"""Hermetic tests for core/charts.py."""

from __future__ import annotations

import base64
from unittest.mock import patch
import pytest

from core.charts import generate_chart


def _create_chart_bars(n_bars: int = 40) -> list[dict]:
    bars = []
    price = 150.0
    for i in range(n_bars):
        price += 0.5
        bars.append({
            "date": f"2026-01-{(i % 28) + 1:02d}",
            "open": price - 0.2,
            "high": price + 1.0,
            "low": price - 0.8,
            "close": price,
            "volume": 2000000 + i * 50000,
        })
    return bars


class TestCharts:
    @pytest.mark.asyncio
    async def test_insufficient_bars_raises_value_error(self):
        short_bars = [
            {"date": "2026-01-01", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
        ]
        with patch("core.charts.get_historical_data", return_value=short_bars):
            with pytest.raises(ValueError, match="Not enough data to chart"):
                await generate_chart("SPY")

    @pytest.mark.asyncio
    async def test_generate_chart_success(self, tmp_path):
        bars = _create_chart_bars(n_bars=35)
        with patch("core.charts.get_historical_data", return_value=bars), \
             patch("core.charts.CHARTS_DIR", tmp_path):
            result = await generate_chart("AAPL", show_emas=True)

            assert result["ticker"] == "AAPL"
            assert result["bars"] == 35
            assert result["period"] == "6mo"
            assert result["interval"] == "1d"
            # 35 bars >= 8, 21, 34 -> emas should have [8, 21, 34]
            assert 8 in result["emas"]
            assert 21 in result["emas"]
            assert 34 in result["emas"]
            assert 55 not in result["emas"]

            # Check base64
            assert len(result["base64"]) > 100
            decoded = base64.b64decode(result["base64"])
            assert decoded.startswith(b"\x89PNG\r\n\x1a\n")

    @pytest.mark.asyncio
    async def test_generate_chart_no_emas(self, tmp_path):
        bars = _create_chart_bars(n_bars=20)
        with patch("core.charts.get_historical_data", return_value=bars), \
             patch("core.charts.CHARTS_DIR", tmp_path):
            result = await generate_chart("TSLA", show_emas=False)
            assert result["ticker"] == "TSLA"
            assert result["emas"] == []
            assert len(result["base64"]) > 100
