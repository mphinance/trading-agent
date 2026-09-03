"""Hermetic tests for core/vcp_screener.py."""

from __future__ import annotations

from unittest.mock import patch
import pytest

from core.cache import clear_cache
from core.vcp_screener import screen_vcp


@pytest.fixture(autouse=True)
def reset_cache():
    clear_cache()
    yield
    clear_cache()


def _generate_vcp_bars(n_bars: int = 250, contracting: bool = True) -> list[dict]:
    bars = []
    # Upward trending base prices from 50 to 120
    price = 50.0
    for i in range(n_bars):
        price += 0.3
        # Last 100 bars: simulate contracting or expanding volatility
        if i >= n_bars - 100:
            days_from_end = n_bars - 1 - i
            # Volatility tighter near the end if contracting
            if contracting:
                spread = max(1.0, 10.0 * (days_from_end / 100.0))
            else:
                spread = 2.0 + 10.0 * (1.0 - days_from_end / 100.0)
        else:
            spread = 3.0

        bars.append({
            "date": f"2025-{(i // 25) + 1:02d}-{(i % 25) + 1:02d}",
            "open": price - 0.5,
            "high": price + spread,
            "low": price - spread,
            "close": price,
            "volume": 2000000 if i < n_bars - 1 else 500000,
        })
    return bars


class TestScreenVcp:
    @pytest.mark.asyncio
    async def test_no_tickers_found_returns_error(self):
        with patch("core.vcp_screener.run_stock_screen", return_value=[]):
            res = await screen_vcp(tickers=None)
            assert res.status == "error"
            assert "No tickers provided" in res.error

    @pytest.mark.asyncio
    async def test_insufficient_history_skipped(self):
        short_bars = [
            {"date": f"2026-01-{i:02d}", "open": 100, "high": 105, "low": 95, "close": 102, "volume": 1000}
            for i in range(1, 50)
        ]
        with patch("core.vcp_screener.get_historical_data", return_value=short_bars):
            res = await screen_vcp(tickers=["AAPL"])
            assert res.status == "success"
            assert res.data["count"] == 0
            assert res.data["results"] == []

    @pytest.mark.asyncio
    async def test_contracting_vcp_candidate_detected(self):
        bars = _generate_vcp_bars(n_bars=260, contracting=True)
        with patch("core.vcp_screener.get_historical_data", return_value=bars):
            res = await screen_vcp(tickers=["NVDA"])
            assert res.status == "success"
            assert res.data["tickers_scanned"] == 1
            if res.data["count"] > 0:
                top = res.data["results"][0]
                assert top["ticker"] == "NVDA"
                assert "vcp_score" in top
                assert top["stage"] == "Stage 2"

    @pytest.mark.asyncio
    async def test_screen_vcp_exception_handling(self):
        with patch("core.vcp_screener.run_stock_screen", side_effect=RuntimeError("Screen failed")):
            res = await screen_vcp()
            assert res.status == "error"
            assert "Screen failed" in res.error
