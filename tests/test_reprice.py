"""Hermetic tests for core/reprice.py -- no real network.

yfinance.Ticker is monkeypatched per-test (the autouse sector stub in
conftest.py only implements .info, not .option_chain()/.history(), so every
test here supplies its own stand-in).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

import core.reprice as reprice_mod
from core.reprice import reprice_option

FUTURE_EXPIRY = (date.today() + timedelta(days=30)).isoformat()


def _chain_df(strike: float, last_price, bid, ask, iv, last_trade_date) -> pd.DataFrame:
    if last_trade_date is None:
        ts = pd.NaT
    elif isinstance(last_trade_date, pd.Timestamp):
        ts = last_trade_date
    else:
        ts = pd.Timestamp(last_trade_date, tz="UTC")
    return pd.DataFrame([{
        "contractSymbol": "TEST260101C00100000",
        "strike": strike,
        "lastPrice": last_price,
        "bid": bid,
        "ask": ask,
        "impliedVolatility": iv,
        "lastTradeDate": ts,
        "volume": 100,
        "openInterest": 500,
    }])


class _FakeChain:
    def __init__(self, calls: pd.DataFrame, puts: pd.DataFrame):
        self.calls = calls
        self.puts = puts


class _FakeTicker:
    """Stands in for yf.Ticker(...) with a fresh, liquid quote by default."""

    def __init__(self, symbol, *, iv=0.80, last_price=2.0, bid=1.95, ask=2.05, stale=False):
        self._symbol = symbol
        self._iv = iv
        self._last_price = last_price
        self._bid = bid
        self._ask = ask
        self._stale = stale

    def option_chain(self, expiration):
        last_trade = (
            pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=10)
            if self._stale else pd.Timestamp.now(tz="UTC")
        )
        calls = _chain_df(100.0, self._last_price, self._bid, self._ask, self._iv, last_trade)
        puts = _chain_df(100.0, self._last_price, self._bid, self._ask, self._iv, last_trade)
        return _FakeChain(calls, puts)

    def history(self, period="6mo"):
        # 60 rows of mildly wiggling OHLC, plenty for composite_rv's period=30.
        idx = pd.date_range(end=pd.Timestamp.today(), periods=60, freq="D")
        base = 100.0
        rows = []
        for i in range(60):
            px = base + (i % 5) - 2
            rows.append({"Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 1000})
        return pd.DataFrame(rows, index=idx)


def _install_fake_yf(monkeypatch, **kwargs):
    def _factory(symbol):
        return _FakeTicker(symbol, **kwargs)
    monkeypatch.setattr(reprice_mod.yf, "Ticker", _factory)


async def _fake_get_historical_data(ticker, period="3mo", interval="1d"):
    idx = pd.date_range(end=pd.Timestamp.today(), periods=60, freq="D")
    out = []
    base = 100.0
    for i, d in enumerate(idx):
        px = base + (i % 5) - 2
        out.append({
            "date": d.isoformat(), "open": px, "high": px + 1, "low": px - 1,
            "close": px, "volume": 1000,
        })
    return out


async def _fake_get_live_price(ticker):
    return 100.0


@pytest.fixture(autouse=True)
def _stub_data_layer(monkeypatch):
    monkeypatch.setattr(reprice_mod, "get_historical_data", _fake_get_historical_data)
    monkeypatch.setattr(reprice_mod, "get_live_price", _fake_get_live_price)


async def test_theoretical_price_is_sane_vs_hand_computed_bs(monkeypatch):
    _install_fake_yf(monkeypatch, iv=0.80, last_price=2.0, bid=1.95, ask=2.05)

    from core.options_greeks import bs_call_price

    result = await reprice_option("TEST", 100.0, FUTURE_EXPIRY, "call", target_spot=100.0)

    assert result["iv_source"] == "market_quote"
    assert result["stale_quote"] is False
    T = result["dte"] / 365.0
    expected = bs_call_price(100.0, 100.0, T, 0.05, 0.80)
    assert abs(result["theoretical"]["price"] - expected) < 0.01


async def test_stale_quote_falls_back_to_realized_vol(monkeypatch):
    _install_fake_yf(monkeypatch, iv=0.12, last_price=2.0, bid=0.0, ask=0.0, stale=True)

    result = await reprice_option("TEST", 100.0, FUTURE_EXPIRY, "call", target_spot=100.0)

    assert result["stale_quote"] is True
    assert result["iv_source"] == "realized_vol_fallback"
    # The garbage 12% market IV must NOT have been used.
    assert result["iv_used_pct"] != 12.0


async def test_iv_override_takes_precedence_over_everything(monkeypatch):
    _install_fake_yf(monkeypatch, iv=0.80, last_price=2.0, bid=1.95, ask=2.05, stale=False)

    result = await reprice_option(
        "TEST", 100.0, FUTURE_EXPIRY, "call", target_spot=100.0, iv_override=0.50,
    )

    assert result["iv_source"] == "override"
    assert result["iv_used_pct"] == pytest.approx(50.0)


async def test_iv_source_reported_correctly_for_market_quote(monkeypatch):
    _install_fake_yf(monkeypatch, iv=0.65, last_price=2.0, bid=1.95, ask=2.05, stale=False)

    result = await reprice_option("TEST", 100.0, FUTURE_EXPIRY, "call", target_spot=100.0)

    assert result["iv_source"] == "market_quote"
    assert result["iv_used_pct"] == pytest.approx(65.0)


async def test_no_matching_contract_falls_back_to_realized_vol(monkeypatch):
    _install_fake_yf(monkeypatch, iv=0.80, last_price=2.0, bid=1.95, ask=2.05)

    # Strike 999 doesn't exist in the fake chain (only 100.0 is there).
    result = await reprice_option("TEST", 999.0, FUTURE_EXPIRY, "call", target_spot=100.0)

    assert result["stale_quote"] is True
    assert result["market_quote"] is None
    assert result["iv_source"] == "realized_vol_fallback"


async def test_defaults_spot_to_live_price_when_not_given(monkeypatch):
    _install_fake_yf(monkeypatch, iv=0.80, last_price=2.0, bid=1.95, ask=2.05)

    result = await reprice_option("TEST", 100.0, FUTURE_EXPIRY, "call")

    assert result["spot_used"] == pytest.approx(100.0)


async def test_bad_expiration_format_is_a_clear_error(monkeypatch):
    _install_fake_yf(monkeypatch, iv=0.80, last_price=2.0, bid=1.95, ask=2.05)

    result = await reprice_option("TEST", 100.0, "not-a-date", "call", target_spot=100.0)

    assert "error" in result
