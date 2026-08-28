"""TickerTrace Pro MCP Server.

Provides institutional ETF tracking, conviction scores, holdings changes,
and cross-fund divergences via FastMCP.
"""

from __future__ import annotations

from typing import Any, Literal
import httpx
from fastmcp import FastMCP

BASE_URL = "https://api.tickertrace.pro"
TIMEOUT = 30.0

mcp = FastMCP(
    "TickerTrace Pro",
    instructions="Institutional ETF holdings changes, conviction scores, sector flow, and cross-fund divergence tracking.",
)


def _fetch(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{BASE_URL}{path}"
    # Remove None values from params
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(url, params=clean_params)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def get_briefing() -> dict[str, Any]:
    """Get the pre-market institutional briefing.

    Returns top institutional buys, top sells, multi-provider convergence,
    active accumulation streaks, and notable new option positions.
    """
    return _fetch("/api/v1/briefing")


@mcp.tool()
def get_signals(
    category: Literal["active-equity", "option-income"] | None = None,
) -> dict[str, Any]:
    """Get full institutional signal payload with conviction-scored buys/sells.

    Parameters
    ----------
    category : Literal["active-equity", "option-income"] | None
        Restrict to one fund family: 'active-equity' (ARK, Avantis, Sprott)
        or 'option-income' (YieldMax, Kurv, REX, Roundhill).
    """
    return _fetch("/api/v1/signals", {"category": category})


@mcp.tool()
def get_institutional_flow(
    period: Literal["daily", "weekly", "monthly"] = "daily",
    limit: int = 25,
) -> dict[str, Any]:
    """Get aggregate institutional accumulation and distribution flow.

    Blends all active equity funds into one AUM-weighted portfolio and reports
    which tickers institutions as a whole are net buying or selling.

    Parameters
    ----------
    period : Literal["daily", "weekly", "monthly"]
        Comparison window (default: 'daily').
    limit : int
        Max tickers to return (default: 25, max: 100).
    """
    return _fetch("/api/v1/institutional", {"period": period, "limit": limit})


@mcp.tool()
def get_institutional_trend(limit: int = 15) -> dict[str, Any]:
    """Get per-ticker institutional accumulation/distribution trend across all horizons (day/week/month).

    Parameters
    ----------
    limit : int
        Max tickers to return (default: 15, max: 50).
    """
    return _fetch("/api/v1/institutional-trend", {"limit": limit})


@mcp.tool()
def get_holdings_changes(
    provider: str | None = None,
    fund: str | None = None,
    direction: Literal["buying", "selling"] | None = None,
    period: Literal["daily", "weekly", "monthly"] = "daily",
    limit: int = 50,
    category: Literal["active-equity", "option-income"] | None = None,
) -> dict[str, Any]:
    """Get institutional position changes filterable by provider, fund, or direction.

    Parameters
    ----------
    provider : str | None
        Filter by fund provider (e.g. 'ARK Invest', 'Avantis', 'YieldMax').
    fund : str | None
        Filter by specific ETF symbol (e.g. 'ARKK', 'AVUV', 'CONY').
    direction : Literal["buying", "selling"] | None
        Filter by trade direction.
    period : Literal["daily", "weekly", "monthly"]
        Comparison window (default: 'daily').
    limit : int
        Max rows to return (default: 50, max: 5000).
    category : Literal["active-equity", "option-income"] | None
        Restrict to fund family.
    """
    return _fetch(
        "/api/v1/changes",
        {
            "provider": provider,
            "fund": fund,
            "direction": direction,
            "period": period,
            "limit": limit,
            "category": category,
        },
    )


@mcp.tool()
def get_divergences(
    category: Literal["active-equity", "option-income"] | None = None,
) -> dict[str, Any]:
    """Get cross-fund divergences where different institutional funds trade the same ticker in opposite directions."""
    return _fetch("/api/v1/divergences", {"category": category})


@mcp.tool()
def get_layering_patterns(
    window_days: int = 7,
    min_funds: int = 3,
    limit: int = 20,
) -> dict[str, Any]:
    """Identify cross-fund layering patterns where 3+ stock-pickers open the SAME new position within days."""
    return _fetch(
        "/api/v1/layering-patterns",
        {"window_days": window_days, "min_funds": min_funds, "limit": limit},
    )


@mcp.tool()
def get_sector_flow(
    category: Literal["active-equity", "option-income"] | None = None,
) -> dict[str, Any]:
    """Get sector-level institutional inflows and outflows."""
    return _fetch("/api/v1/sectors", {"category": category})


@mcp.tool()
def get_stock_activity(ticker: str) -> dict[str, Any]:
    """Get complete institutional activity for a single stock: current fund holders, weight changes, and institutional A/D trend.

    Parameters
    ----------
    ticker : str
        Stock ticker symbol (e.g. 'AAPL', 'NVDA', 'TSLA', 'AVGO').
    """
    return _fetch(f"/api/v1/stock/{ticker.upper()}")


@mcp.tool()
def get_fund_detail(fund: str) -> dict[str, Any]:
    """Get details for a specific ETF fund: top holdings, options count, and AUM.

    Parameters
    ----------
    fund : str
        Fund symbol (e.g. 'ARKK', 'AVUV', 'NVDY', 'QDTE').
    """
    return _fetch(f"/api/v1/fund/{fund.upper()}")


@mcp.tool()
def list_all_funds(
    category: Literal["active-equity", "option-income"] | None = None,
) -> dict[str, Any]:
    """List all tracked institutional funds enriched with holdings counts and top holdings."""
    return _fetch("/api/v1/funds", {"category": category})


@mcp.tool()
def list_all_tickers(
    limit: int = 100,
    sort: Literal["funds", "weight"] = "funds",
    category: Literal["active-equity", "option-income"] | None = None,
) -> dict[str, Any]:
    """List the most widely-held underlying tickers across all tracked funds."""
    return _fetch(
        "/api/v1/tickers",
        {"limit": limit, "sort": sort, "category": category},
    )


@mcp.tool()
def get_income_overview() -> dict[str, Any]:
    """Get coverage and structural classification for all option-income funds (covered-call, synthetic, leap-proxy, swap)."""
    return _fetch("/api/v1/income")


@mcp.tool()
def get_income_fund_detail(fund: str) -> dict[str, Any]:
    """Get an option-income fund's full book with call coverage, moneyness, and options overlay details."""
    return _fetch(f"/api/v1/income/{fund.upper()}")


@mcp.tool()
def get_options_listings() -> dict[str, Any]:
    """Get CBOE options scanner daily diff: newly optionable stocks and weekly option listings."""
    return _fetch("/api/v1/options-listings")


@mcp.tool()
def get_signal_performance() -> dict[str, Any]:
    """Get historical backtest performance statistics for TickerTrace conviction signals."""
    return _fetch("/api/v1/signal-performance")


@mcp.tool()
def get_global_stats() -> dict[str, Any]:
    """Get global tracking stats: total funds tracked, unique underlyings, and options counts."""
    return _fetch("/api/v1/stats")


if __name__ == "__main__":
    mcp.run(transport="stdio")
