"""Tests for Module 4: Walk-Forward Backtester & Strategy Presets."""

from __future__ import annotations

import pytest
import numpy as np
import pandas as pd
from mcp_server.backtest import (
    backtest_strategy,
    walk_forward_test,
    sweep_strategy,
    PRESET_STRATEGIES,
)


@pytest.fixture
def mock_ohlcv(monkeypatch):
    """Generate synthetic trending and mean-reverting OHLCV data for backtesting."""
    n = 200
    dates = pd.date_range("2024-01-01", periods=n, freq="B").strftime("%Y-%m-%d").tolist()

    # Generate synthetic price series with trend and pullbacks
    t = np.linspace(0, 10, n)
    trend = 100.0 + (t * 5.0) + (np.sin(t * 2) * 8.0)
    
    records = []
    for i in range(n):
        close_p = float(trend[i])
        high_p = close_p + 1.5
        low_p = close_p - 1.5
        open_p = close_p - 0.5
        records.append({
            "date": dates[i],
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": 1_000_000,
        })

    async def mock_get_historical_data(ticker, period="1y", interval="1d"):
        return records

    monkeypatch.setattr("mcp_server.backtest.get_historical_data", mock_get_historical_data)
    return records


@pytest.mark.asyncio
async def test_backtest_strategy_with_bounce_2_preset(mock_ohlcv):
    """Verify backtest_strategy executes with the Tao of Trading Bounce 2.0 preset."""
    assert "bounce_2_pullback" in PRESET_STRATEGIES

    res = await backtest_strategy(
        ticker="NVDA",
        strategy_name="bounce_2_pullback",
        period="1y",
        stop_loss_pct=5.0,
        take_profit_pct=10.0,
    )

    assert res["ticker"] == "NVDA"
    assert res["strategy_name"] == "bounce_2_pullback"
    assert "total_return_pct" in res
    assert "sharpe_ratio" in res
    assert "max_drawdown_pct" in res
    assert "profit_factor" in res
    assert "win_rate_pct" in res
    assert "trades" in res
    assert "equity_curve" in res


@pytest.mark.asyncio
async def test_walk_forward_test_validation(mock_ohlcv):
    """Verify walk_forward_test splits data into folds and produces stability metrics."""
    res = await walk_forward_test(
        ticker="SPY",
        strategy_name="ema_crossover",
        total_period="2y",
        n_folds=4,
    )

    assert res["ticker"] == "SPY"
    assert res["n_folds"] == 4
    assert len(res["folds"]) >= 2
    assert "summary" in res
    assert "avg_return_pct" in res["summary"]
    assert "consistency_pct" in res["summary"]


@pytest.mark.asyncio
async def test_sweep_strategy_universe(mock_ohlcv):
    """Verify sweep_strategy evaluates multiple tickers across a strategy preset."""
    tickers = ["AAPL", "NVDA", "TSLA"]
    res = await sweep_strategy(
        tickers=tickers,
        strategy_name="bounce_2_pullback",
        period="1y",
    )

    assert res["total_tickers"] == 3
    assert len(res["results"]) == 3
    assert all("total_return_pct" in r for r in res["results"])
