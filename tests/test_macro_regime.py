"""Hermetic tests for core/macro_regime.py."""

from __future__ import annotations

from unittest.mock import patch
import pytest

from core.macro_regime import detect_macro_regime


from datetime import date, timedelta

def _create_series_records(start_val: float, end_val: float, n_days: int = 60) -> list[dict]:
    step = (end_val - start_val) / n_days
    base_date = date(2026, 1, 1)
    records = []
    for i in range(n_days):
        dt = (base_date + timedelta(days=i)).isoformat()
        records.append({
            "date": dt,
            "close": start_val + i * step,
            "open": start_val + i * step,
            "high": start_val + i * step + 1,
            "low": start_val + i * step - 1,
            "volume": 1000000,
        })
    return records


class TestMacroRegime:
    @pytest.mark.asyncio
    async def test_no_data_returns_error(self):
        with patch("core.macro_regime.get_historical_data", return_value=[]):
            res = await detect_macro_regime()
            assert res.status == "error"
            assert "Failed to compute macro ratios" in res.error

    @pytest.mark.asyncio
    async def test_goldilocks_regime(self):
        # Goldilocks: cycl (XLY/XLP) Rising, risk (GLD/SPY) Falling, yield_c (TLT/SHY) Falling
        data_map = {
            # RSP / SPY
            "RSP": _create_series_records(150, 155),
            "SPY": _create_series_records(480, 520),  # SPY rising faster -> GLD/SPY falling
            # TLT / SHY -> TLT falling, SHY flat -> yield_c Falling
            "TLT": _create_series_records(100, 90),
            "SHY": _create_series_records(80, 80),
            # GLD / SPY -> GLD flat, SPY rising -> risk Falling
            "GLD": _create_series_records(200, 200),
            # XLY / XLP -> XLY rising, XLP flat -> cycl Rising
            "XLY": _create_series_records(150, 200),
            "XLP": _create_series_records(70, 70),
        }

        async def fake_get_historical_data(ticker, period="6mo", interval="1d"):
            return data_map.get(ticker, [])

        with patch("core.macro_regime.get_historical_data", side_effect=fake_get_historical_data):
            res = await detect_macro_regime()
            assert res.status == "success"
            data = res.data
            assert "regime" in data
            assert "ratios" in data
            assert "summary" in data
            assert "Goldilocks" in data["regime"]

    @pytest.mark.asyncio
    async def test_risk_off_regime(self):
        # Risk-off: risk (GLD/SPY) Rising, cycl (XLY/XLP) Falling
        data_map = {
            "RSP": _create_series_records(150, 140),
            "SPY": _create_series_records(500, 450),
            "TLT": _create_series_records(90, 95),
            "SHY": _create_series_records(80, 80),
            "GLD": _create_series_records(200, 230),  # GLD up, SPY down -> GLD/SPY Rising
            "XLY": _create_series_records(200, 160),  # XLY down, XLP up -> cycl Falling
            "XLP": _create_series_records(70, 75),
        }

        async def fake_get_historical_data(ticker, period="6mo", interval="1d"):
            return data_map.get(ticker, [])

        with patch("core.macro_regime.get_historical_data", side_effect=fake_get_historical_data):
            res = await detect_macro_regime()
            assert res.status == "success"
            assert "Deflationary / Risk-Off" in res.data["regime"]
