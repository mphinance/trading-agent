"""Hermetic tests for core/market_top.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
import pytest

from core.cache import clear_cache
from core.market_top import detect_market_top
from core.schema import SignalResult


@pytest.fixture(autouse=True)
def reset_cache():
    clear_cache()
    yield
    clear_cache()


def _create_index_bars(n_bars: int = 30, distribute: bool = False) -> list[dict]:
    bars = []
    price = 500.0
    vol = 50000000
    for i in range(n_bars):
        if distribute and i % 3 == 0:
            # Drop > 0.2% on higher volume
            price -= 2.0
            vol += 1000000
            high = price + 3.0
            low = price - 2.0
            close = price
        else:
            price += 1.0
            vol = 40000000
            high = price + 1.0
            low = price - 0.5
            close = price

        bars.append({
            "date": f"2026-01-{(i % 28) + 1:02d}",
            "open": price - 0.5,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
        })
    return bars


class TestMarketTop:
    @pytest.mark.asyncio
    async def test_no_index_data_returns_error(self):
        with patch("core.market_top.get_historical_data", side_effect=RuntimeError("Network down")):
            res = await detect_market_top()
            assert res.status == "error"
            assert "Could not fetch index data" in res.error

    @pytest.mark.asyncio
    async def test_healthy_market_top_verdict(self):
        bars = _create_index_bars(n_bars=30, distribute=False)
        fake_leaders = [{"ticker": f"SYM{i}"} for i in range(50)]

        sector_data = SignalResult.success([
            {"sector": "Technology", "sentiment_score": 5.0},
            {"sector": "Utilities", "sentiment_score": 1.0},
        ])

        with patch("core.market_top.get_historical_data", new_callable=AsyncMock, return_value=bars), \
             patch("core.market_top.run_stock_screen", new_callable=AsyncMock, return_value=fake_leaders), \
             patch("core.market_top.get_sector_flow", new_callable=AsyncMock, return_value=sector_data):
            res = await detect_market_top()
            assert res.status == "success"
            data = res.data
            assert data["verdict"] == "HEALTHY"
            assert data["score"] < 26
            assert data["defensive_rotation_detected"] is False

    @pytest.mark.asyncio
    async def test_danger_market_top_verdict(self):
        # Many distribution days
        bars = _create_index_bars(n_bars=30, distribute=True)
        # Low leadership
        fake_leaders = [{"ticker": "SYM1"}]

        # Defensive sector rotation: Utilities > Tech
        sector_data = SignalResult.success([
            {"sector": "Utilities", "sentiment_score": 10.0},
            {"sector": "Consumer Staples", "sentiment_score": 5.0},
            {"sector": "Technology", "sentiment_score": 1.0},
        ])

        with patch("core.market_top.get_historical_data", new_callable=AsyncMock, return_value=bars), \
             patch("core.market_top.run_stock_screen", new_callable=AsyncMock, return_value=fake_leaders), \
             patch("core.market_top.get_sector_flow", new_callable=AsyncMock, return_value=sector_data):
            res = await detect_market_top()
            assert res.status == "success"
            data = res.data
            assert data["verdict"] in ("DISTRIBUTION", "DANGER")
            assert data["score"] >= 51
            assert data["defensive_rotation_detected"] is True
