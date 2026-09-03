"""Hermetic tests for core/cache.py."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
import pytest

from core.cache import (
    ET,
    _make_key,
    clear_cache,
    get_cache_stats,
    get_market_status,
    get_ttl,
    is_market_open,
    smart_cache,
)


@pytest.fixture(autouse=True)
def reset_cache_state():
    clear_cache()
    yield
    clear_cache()


class TestMarketSchedule:
    def test_market_open_hours(self):
        # Wednesday at 11:00 AM ET
        dt_open = datetime(2026, 1, 14, 11, 0, 0, tzinfo=ET)
        with patch("core.cache.datetime") as mock_dt:
            mock_dt.now.return_value = dt_open
            assert is_market_open() is True
            assert get_market_status() == "open"
            assert get_ttl(open_ttl=60, closed_ttl=300) == 60

    def test_market_closed_evening(self):
        # Wednesday at 8:00 PM ET
        dt_closed = datetime(2026, 1, 14, 20, 0, 0, tzinfo=ET)
        with patch("core.cache.datetime") as mock_dt:
            mock_dt.now.return_value = dt_closed
            assert is_market_open() is False
            assert get_market_status() == "closed"
            assert get_ttl(open_ttl=60, closed_ttl=300) == 300

    def test_market_weekend(self):
        # Saturday at 1:00 PM ET
        dt_weekend = datetime(2026, 1, 17, 13, 0, 0, tzinfo=ET)
        with patch("core.cache.datetime") as mock_dt:
            mock_dt.now.return_value = dt_weekend
            assert is_market_open() is False
            assert get_market_status() == "weekend"
            assert get_ttl() == 86400

    def test_market_holiday(self):
        # Jan 19, 2026 (MLK Day) at 11:00 AM ET
        dt_holiday = datetime(2026, 1, 19, 11, 0, 0, tzinfo=ET)
        with patch("core.cache.datetime") as mock_dt:
            mock_dt.now.return_value = dt_holiday
            assert is_market_open() is False
            assert get_market_status() == "holiday"
            assert get_ttl() == 86400


class TestKeyGen:
    def test_make_key_deterministic(self):
        k1 = _make_key("my_func", ("AAPL", 10), {"limit": 5, "debug": True})
        k2 = _make_key("my_func", ("AAPL", 10), {"debug": True, "limit": 5})
        assert k1 == k2

        k3 = _make_key("my_func", ("TSLA", 10), {"limit": 5, "debug": True})
        assert k1 != k3


class TestSmartCacheDecorator:
    @pytest.mark.asyncio
    async def test_cache_hit_and_miss(self):
        calls = 0

        @smart_cache(open_ttl=60, closed_ttl=120)
        async def fetch_data(symbol: str):
            nonlocal calls
            calls += 1
            return f"result_{symbol}_{calls}"

        # Miss
        val1 = await fetch_data("AAPL")
        assert val1 == "result_AAPL_1"
        assert calls == 1

        # Hit
        val2 = await fetch_data("AAPL")
        assert val2 == "result_AAPL_1"
        assert calls == 1

        # Different arg -> Miss
        val3 = await fetch_data("MSFT")
        assert val3 == "result_MSFT_2"
        assert calls == 2

        stats = get_cache_stats()
        assert stats["total_entries"] == 2
        assert stats["active_entries"] == 2

    @pytest.mark.asyncio
    async def test_request_coalescing(self):
        upstream_calls = 0

        @smart_cache(open_ttl=60, closed_ttl=120)
        async def slow_fetch(symbol: str):
            nonlocal upstream_calls
            upstream_calls += 1
            await asyncio.sleep(0.05)
            return f"val_{symbol}"

        # Run 5 concurrent calls for same key
        tasks = [slow_fetch("NVDA") for _ in range(5)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 5
        assert all(r == "val_NVDA" for r in results)
        assert upstream_calls == 1  # Only 1 upstream request made

    @pytest.mark.asyncio
    async def test_exception_propagation(self):
        @smart_cache(open_ttl=60, closed_ttl=120)
        async def failing_call():
            raise ValueError("Something broke")

        with pytest.raises(ValueError, match="Something broke"):
            await failing_call()

        stats = get_cache_stats()
        assert stats["total_entries"] == 0
