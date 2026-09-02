"""
mcp_server.registry: Modular tool registration for FastMCP.

Allows external MCP servers (such as supermcp on Coolify) to register
Momentum trading tools selectively by tier or all at once.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _out(res: Any) -> Any:
    if hasattr(res, "model_dump"):
        return res.model_dump()
    if hasattr(res, "to_dict"):
        return res.to_dict()
    if hasattr(res, "dict"):
        return res.dict()
    return res


# ═══════════════════════════════════════════════════════════════════════════════
# TIER 1: PURE REST, FLOW, SEC EDGAR, NEWS, & SIZING (No Heavy Deps)
# ═══════════════════════════════════════════════════════════════════════════════

def register_tier1_tools(mcp: Any) -> list[str]:
    """Register lightweight REST, SEC, News, TDPro Flow, and Sizing tools."""
    from mcp_server.traderdaddy import (
        get_market_pulse as _get_market_pulse,
        get_unusual_activity as _get_unusual_activity,
        get_sector_flow as _get_sector_flow,
        get_signals as _get_signals,
        get_gex_overview as _get_gex_overview,
        get_earnings_calendar as _get_earnings_calendar,
        get_put_call_ratios as _get_put_call_ratios,
        get_market_stats as _get_market_stats,
        get_politician_trades as _get_politician_trades,
        get_earnings_flow as _get_earnings_flow,
    )
    from mcp_server.fundamentals import get_fundamentals as _get_fundamentals
    from mcp_server.edgar_tools import (
        get_sec_filings as _get_sec_filings,
        get_sec_financials as _get_sec_financials,
        get_shares_outstanding as _get_shares_outstanding,
        get_stakes_held as _get_stakes_held,
    )
    from mcp_server.news import (
        fetch_ticker_news as _fetch_ticker_news,
        extract_article_text as _extract_article_text,
    )
    from mcp_server.position_sizer import calculate_position_size as _calculate_position_size
    from mcp_server.warmer import get_alpha_signals as _get_alpha_signals

    # --- TDPro Flow & Sentiment ---
    @mcp.tool()
    async def get_market_pulse() -> dict[str, Any]:
        """AI-generated market sentiment with options flow score (-7 to +7)."""
        return _out(await _get_market_pulse())

    @mcp.tool()
    async def get_market_stats() -> dict[str, Any]:
        """Market-wide put/call ratios and sentiment indicators."""
        return _out(await _get_market_stats())

    @mcp.tool()
    async def get_put_call_ratios(ticker: str = "SPY") -> dict[str, Any]:
        """Put/call ratios for SPY, QQQ, IWM (or any ticker)."""
        return _out(await _get_put_call_ratios(ticker=ticker))

    @mcp.tool()
    async def get_sector_flow() -> dict[str, Any]:
        """Sector-by-sector options flow with bullish/bearish sentiment."""
        return _out(await _get_sector_flow())

    @mcp.tool()
    async def get_unusual_activity() -> dict[str, Any]:
        """Unusual options flow feed — institutional trades, premium, conviction."""
        return _out(await _get_unusual_activity())

    @mcp.tool()
    async def get_signals() -> dict[str, Any]:
        """Breakout and continuation signals with technical indicator data."""
        return _out(await _get_signals())

    @mcp.tool()
    async def get_gex_overview() -> dict[str, Any]:
        """Gamma Exposure (GEX) for SPY/QQQ/IWM. GEX flip level = regime boundary."""
        return _out(await _get_gex_overview())

    @mcp.tool()
    async def get_earnings_calendar() -> dict[str, Any]:
        """Weekly earnings calendar — who reports this week."""
        return _out(await _get_earnings_calendar())

    @mcp.tool()
    async def get_earnings_flow() -> dict[str, Any]:
        """Pre-earnings options flow — institutional positioning ahead of earnings."""
        return _out(await _get_earnings_flow())

    @mcp.tool()
    async def get_politician_trades() -> dict[str, Any]:
        """Congressional stock trading disclosures."""
        return _out(await _get_politician_trades())

    @mcp.tool()
    async def get_alpha_signals(
        ticker: str | None = None,
        signal_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get recent alpha signals detected by the background signal factory."""
        return _out(await _get_alpha_signals(ticker=ticker, signal_type=signal_type, limit=limit))

    # --- SEC & Fundamentals ---
    @mcp.tool()
    async def get_fundamentals(ticker: str) -> dict[str, Any]:
        """Get fundamental data: P/E, EPS, revenue growth, margin, short interest."""
        return _out(await _get_fundamentals(ticker=ticker))

    @mcp.tool()
    async def get_sec_filings(
        ticker: str, months: int = 12, forms: list[str] | None = None
    ) -> dict[str, Any]:
        """SEC EDGAR filing index straight from the primary source."""
        return _out(await _get_sec_filings(ticker=ticker, months=months, forms=forms))

    @mcp.tool()
    async def get_sec_financials(
        ticker: str, periods: int = 8, annual: bool = True
    ) -> dict[str, Any]:
        """Multi-period financials from SEC XBRL, including the accrual gap."""
        return _out(await _get_sec_financials(ticker=ticker, periods=periods, annual=annual))

    @mcp.tool()
    async def get_shares_outstanding(ticker: str) -> dict[str, Any]:
        """Cover-page share count straight from the 10-Q/10-K filing."""
        return _out(await _get_shares_outstanding(ticker=ticker))

    @mcp.tool()
    async def get_stakes_held(ticker: str, months: int = 24) -> dict[str, Any]:
        """AS-FILER 13D/13G: stakes this company holds in OTHER public companies."""
        return _out(await _get_stakes_held(ticker=ticker, months=months))

    # --- News ---
    @mcp.tool()
    async def fetch_ticker_news(ticker: str, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch recent financial news headlines for a stock from RSS feeds."""
        return _out(await _fetch_ticker_news(ticker=ticker, limit=limit))

    @mcp.tool()
    async def extract_article_text(url: str) -> dict[str, Any]:
        """Extract the full-text body of a news article. Strips ads and nav."""
        return _out(await _extract_article_text(url=url))

    # --- Position Sizing ---
    @mcp.tool()
    async def calculate_position_size(
        ticker: str,
        account_size: float,
        risk_pct: float = 1.0,
        entry_price: float | None = None,
        stop_price: float | None = None,
        method: str = "fixed_fractional",
    ) -> dict[str, Any]:
        """Calculate risk-based position size using Fixed Fractional, ATR, or Kelly methods."""
        res = await _calculate_position_size(
            ticker=ticker,
            account_size=account_size,
            risk_pct=risk_pct,
            entry_price=entry_price,
            stop_price=stop_price,
            method=method,
        )
        return _out(res)

    registered = [
        "get_market_pulse", "get_market_stats", "get_put_call_ratios", "get_sector_flow",
        "get_unusual_activity", "get_signals", "get_gex_overview", "get_earnings_calendar",
        "get_earnings_flow", "get_politician_trades", "get_alpha_signals", "get_fundamentals",
        "get_sec_filings", "get_sec_financials", "get_shares_outstanding", "get_stakes_held",
        "fetch_ticker_news", "extract_article_text", "calculate_position_size",
    ]
    logger.info("Registered %d Tier 1 tools", len(registered))
    return registered


# ═══════════════════════════════════════════════════════════════════════════════
# TIER 2: REGIME, BREADTH, SCREENERS & TECHNICALS
# ═══════════════════════════════════════════════════════════════════════════════

def register_tier2_tools(mcp: Any) -> list[str]:
    """Register Market Regime, Breadth, Screeners, and Technical Analysis tools."""
    from core.screener import (
        run_stock_screen as _run_stock_screen,
        run_custom_screen as _run_custom_screen,
    )
    from core.data import get_historical_data as _get_historical_data
    from core.technicals import analyze_technicals as _analyze_technicals
    from mcp_server.tv_analysis import get_tv_analysis as _get_tv_analysis
    from core.charts import generate_chart as _generate_chart
    from mcp_server.alpha_cards import generate_alpha_card as _generate_alpha_card
    from core.vcp_screener import screen_vcp as _screen_vcp
    from mcp_server.pead_screener import screen_pead as _screen_pead
    from mcp_server.canslim_screener import screen_canslim as _screen_canslim
    from core.market_top import detect_market_top as _detect_market_top
    from mcp_server.ftd_detector import detect_ftd as _detect_ftd
    from mcp_server.pair_trade import analyze_pair as _analyze_pair
    from mcp_server.scenario import (
        analyze_scenario as _analyze_scenario,
        model_price_distribution as _model_price_distribution,
    )
    from mcp_server.exposure import get_exposure_recommendation as _get_exposure_recommendation
    from mcp_server.environment import get_market_environment as _get_market_environment
    from core.macro_regime import detect_macro_regime as _detect_macro_regime
    from mcp_server.breadth import analyze_breadth as _analyze_breadth
    from mcp_server.uptrend import analyze_uptrend_participation as _analyze_uptrend_participation
    from mcp_server.themes import detect_themes as _detect_themes
    from mcp_server.earnings_analyzer import analyze_recent_gap as _analyze_recent_gap
    from mcp_server.bubble import detect_bubble_risk as _detect_bubble_risk
    from mcp_server.warmer import WARM_TICKERS

    @mcp.tool()
    async def run_stock_screen(preset: str = "most_active", limit: int = 25) -> dict[str, Any]:
        """Run a stock screen using TradingView's scanner (22 presets)."""
        return _out(await _run_stock_screen(preset=preset, limit=limit))

    @mcp.tool()
    async def run_custom_screen(
        filters: list[dict[str, Any]],
        sort_by: str = "volume",
        sort_ascending: bool = False,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Build a custom stock screen with dynamic filter conditions."""
        return _out(await _run_custom_screen(filters=filters, sort_by=sort_by, sort_ascending=sort_ascending, limit=limit))

    @mcp.tool()
    async def get_historical_data(
        ticker: str, period: str = "3mo", interval: str = "1d"
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV historical price data for a stock."""
        return _out(await _get_historical_data(ticker=ticker, period=period, interval=interval))

    @mcp.tool()
    async def analyze_technicals(ticker: str, period: str = "1y") -> dict[str, Any]:
        """Compute 24 technical indicators (EMA stack, RSI, MACD, ADX, ATR, Bollinger)."""
        return _out(await _analyze_technicals(ticker=ticker, period=period))

    @mcp.tool()
    async def get_tv_analysis(ticker: str) -> dict[str, Any]:
        """Get TradingView 26-indicator technical consensus for a ticker."""
        return _out(await _get_tv_analysis(ticker=ticker))

    @mcp.tool()
    async def generate_chart(
        ticker: str, period: str = "6mo", interval: str = "1d",
        style: str = "dark", show_emas: bool = True,
    ) -> dict[str, str]:
        """Generate a candlestick chart with EMA overlays (8/21/34/55/89)."""
        return _out(await _generate_chart(
            ticker=ticker, period=period, interval=interval,
            style=style, show_emas=show_emas,
        ))

    @mcp.tool()
    async def generate_alpha_card(ticker: str, sam_take: str = "") -> dict[str, Any]:
        """Generate a shareable Alpha Card HTML visual combining technicals and TV analysis."""
        technicals = None
        tv_data = None
        try:
            technicals = await _analyze_technicals(ticker=ticker)
        except Exception:
            pass
        try:
            tv_data = await _get_tv_analysis(ticker=ticker)
        except Exception:
            pass

        html = _generate_alpha_card(
            ticker=ticker,
            technicals=technicals,
            tv_analysis=tv_data,
            sam_take=sam_take,
        )
        return {"ticker": ticker, "html_length": len(html), "html": html[:500] + "..."}

    @mcp.tool()
    async def screen_vcp(tickers: list[str] | None = None, max_tickers: int = 50) -> dict[str, Any]:
        """Screen for stocks forming a Volatility Contraction Pattern (VCP)."""
        return _out(await _screen_vcp(tickers=tickers, max_tickers=max_tickers))

    @mcp.tool()
    async def screen_pead(lookback_days: int = 10) -> dict[str, Any]:
        """Screen for Post-Earnings Announcement Drift (PEAD) setups."""
        return _out(await _screen_pead(lookback_days=lookback_days))

    @mcp.tool()
    async def screen_canslim(tickers: list[str] | None = None, max_tickers: int = 30) -> dict[str, Any]:
        """Screen for growth stocks matching CANSLIM criteria."""
        return _out(await _screen_canslim(tickers=tickers, max_tickers=max_tickers))

    @mcp.tool()
    async def detect_market_top() -> dict[str, Any]:
        """Detect market topping signals using distribution days and leadership trends."""
        return _out(await _detect_market_top())

    @mcp.tool()
    async def detect_ftd() -> dict[str, Any]:
        """Detect Follow-Through Days (FTDs) on major indices to confirm market bottoms."""
        return _out(await _detect_ftd())

    @mcp.tool()
    async def analyze_pair(ticker_a: str, ticker_b: str, lookback: int = 60) -> dict[str, Any]:
        """Analyze a pair of stocks for statistical arbitrage."""
        return _out(await _analyze_pair(ticker_a=ticker_a, ticker_b=ticker_b, lookback=lookback))

    @mcp.tool()
    async def analyze_scenario(ticker: str, catalyst: str, timeframe: str = "30d") -> dict[str, Any]:
        """Generate bull/base/bear scenarios for a ticker based on a catalyst."""
        return _out(await _analyze_scenario(ticker=ticker, catalyst=catalyst, timeframe=timeframe))

    @mcp.tool()
    async def model_price_distribution(ticker: str, days_forward: int = 30) -> dict[str, Any]:
        """Compute statistical price targets using historical volatility."""
        return _out(await _model_price_distribution(ticker=ticker, days_forward=days_forward))

    @mcp.tool()
    async def get_exposure_recommendation() -> dict[str, Any]:
        """Get a market exposure recommendation (0-100% capital deployment)."""
        return _out(await _get_exposure_recommendation())

    @mcp.tool()
    async def get_market_environment() -> dict[str, Any]:
        """Get a cross-asset market environment report across asset classes."""
        return _out(await _get_market_environment())

    @mcp.tool()
    async def detect_macro_regime(lookback: int = 90) -> dict[str, Any]:
        """Detect structural market regime (Growth, Inflation, Deflation, Goldilocks)."""
        return _out(await _detect_macro_regime(lookback=lookback))

    @mcp.tool()
    async def analyze_breadth() -> dict[str, Any]:
        """Get a comprehensive market breadth health score (0-100)."""
        return _out(await _analyze_breadth())

    @mcp.tool()
    async def analyze_uptrend_participation() -> dict[str, Any]:
        """Measure market participation in structural uptrends (% > EMA50/200)."""
        return _out(await _analyze_uptrend_participation())

    @mcp.tool()
    async def detect_themes(lookback: int = 20) -> dict[str, Any]:
        """Identify trending market themes by clustering thematic ETF performance."""
        return _out(await _detect_themes(lookback=lookback))

    @mcp.tool()
    async def analyze_recent_gap(ticker: str) -> dict[str, Any]:
        """Score the most recent overnight gap reaction (0-100) for a ticker."""
        return _out(await _analyze_recent_gap(ticker=ticker))

    @mcp.tool()
    async def detect_bubble_risk() -> dict[str, Any]:
        """Assess current market euphoria and bubble risk (0-15 score)."""
        return _out(await _detect_bubble_risk())

    @mcp.tool()
    async def get_momentum_pulse(tickers: list[str] | None = None) -> dict[str, Any]:
        """Calculate real-time momentum scores (0-100) using EMA stack, RSI, and ADX."""
        target_tickers = tickers or WARM_TICKERS
        results = []
        for t in target_tickers[:30]:
            try:
                tech = await _analyze_technicals(ticker=t)
                d = tech.data if hasattr(tech, 'data') and isinstance(tech.data, dict) else tech if isinstance(tech, dict) else None
                if not d:
                    continue
                score = 0.0
                max_score = 43.0
                adx = d.get("adx_14")
                if adx is not None:
                    if adx >= 40: score += 18
                    elif adx >= 30: score += 14
                    elif adx >= 25: score += 10
                    elif adx >= 20: score += 5
                rsi = d.get("rsi_14")
                if rsi is not None:
                    if 50 <= rsi <= 60: score += 15
                    elif 45 <= rsi < 50: score += 12
                    elif 60 < rsi <= 65: score += 10
                    elif 65 < rsi <= 70: score += 5
                    elif 40 <= rsi < 45: score += 5
                    elif rsi > 70: score -= 5
                    elif rsi < 30: score -= 10
                stack_bullish = d.get("ema_stack_bullish")
                if stack_bullish is True:
                    score += 10
                elif stack_bullish is False:
                    ema_vals = [d.get(f"ema_{l}") for l in [8, 21, 34, 55, 89]]
                    if all(v is not None for v in ema_vals):
                        if all(ema_vals[i] < ema_vals[i+1] for i in range(len(ema_vals)-1)):
                            score -= 10
                normalized = ((score + max_score) / (2 * max_score)) * 100
                normalized = max(0, min(100, normalized))
                if normalized >= 70: label = "🟢 STRONG"
                elif normalized >= 55: label = "🟡 MODERATE"
                elif normalized >= 40: label = "⚪ NEUTRAL"
                elif normalized >= 25: label = "🟠 WEAK"
                else: label = "🔴 EXHAUSTED"
                results.append({
                    "ticker": t,
                    "pulse_score": round(normalized, 1),
                    "label": label,
                    "rsi_14": rsi,
                    "adx_14": adx,
                    "ema_stack_bullish": stack_bullish,
                    "close": d.get("close"),
                })
            except Exception:
                continue
        results.sort(key=lambda x: x["pulse_score"], reverse=True)
        return {
            "pulse": results,
            "count": len(results),
            "strongest": results[0]["ticker"] if results else None,
            "weakest": results[-1]["ticker"] if results else None,
        }

    registered = [
        "run_stock_screen", "run_custom_screen", "get_historical_data", "analyze_technicals",
        "get_tv_analysis", "generate_chart", "generate_alpha_card", "screen_vcp", "screen_pead",
        "screen_canslim", "detect_market_top", "detect_ftd", "analyze_pair", "analyze_scenario",
        "model_price_distribution", "get_exposure_recommendation", "get_market_environment",
        "detect_macro_regime", "analyze_breadth", "analyze_uptrend_participation", "detect_themes",
        "analyze_recent_gap", "detect_bubble_risk", "get_momentum_pulse",
    ]
    logger.info("Registered %d Tier 2 tools", len(registered))
    return registered


# ═══════════════════════════════════════════════════════════════════════════════
# TIER 3: OPTIONS VOPR ANALYTICS ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def register_tier3_tools(mcp: Any) -> list[str]:
    """Register VoPR Options Analytics tools."""
    from core.options import (
        analyze_options_setup as _analyze_options_setup,
        find_best_to_sell as _find_best_to_sell,
        find_best_to_buy as _find_best_to_buy,
        sweep_setups as _sweep_setups,
    )

    @mcp.tool()
    async def analyze_options_setup(
        ticker: str, option_type: str = "put", dte: int = 30,
        budget: float | None = None, contracts: int = 1,
        iv_override: float | None = None,
    ) -> dict[str, Any]:
        """VoPR™ engine: composite realized vol (4 estimators), VRP ratio, Delta/Theta, A-F grade."""
        return _out(await _analyze_options_setup(
            ticker=ticker, option_type=option_type, dte=dte,
            budget=budget, contracts=contracts, iv_override=iv_override,
        ))

    @mcp.tool()
    async def find_best_to_sell(ticker: str, budget: float | None = None) -> dict[str, Any]:
        """Auto-find the best puts and calls to SELL across 7-45 DTE."""
        return _out(await _find_best_to_sell(ticker=ticker, budget=budget))

    @mcp.tool()
    async def find_best_to_buy(ticker: str, budget: float | None = None) -> dict[str, Any]:
        """Auto-find the best directional option to BUY across 21-60 DTE."""
        return _out(await _find_best_to_buy(ticker=ticker, budget=budget))

    @mcp.tool()
    async def sweep_setups(
        tickers: list[str] | None = None, budget: float | None = None,
        max_tickers: int = 10,
    ) -> dict[str, Any]:
        """Opportunity Board: scan multiple tickers for best options trades."""
        return _out(await _sweep_setups(tickers=tickers, budget=budget, max_tickers=max_tickers))

    registered = [
        "analyze_options_setup", "find_best_to_sell", "find_best_to_buy", "sweep_setups"
    ]
    logger.info("Registered %d Tier 3 tools", len(registered))
    return registered


# ═══════════════════════════════════════════════════════════════════════════════
# REGISTER ALL TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def register_momentum_tools(mcp: Any, include_tiers: tuple[int, ...] = (1, 2, 3)) -> list[str]:
    """
    Register Momentum tools onto an existing FastMCP server instance.
    
    Args:
        mcp: FastMCP instance (e.g. `supermcp`'s `mcp`).
        include_tiers: Which tiers to register (default: Tier 1, 2, and 3).
    """
    all_registered: list[str] = []
    if 1 in include_tiers:
        all_registered.extend(register_tier1_tools(mcp))
    if 2 in include_tiers:
        all_registered.extend(register_tier2_tools(mcp))
    if 3 in include_tiers:
        all_registered.extend(register_tier3_tools(mcp))
    return all_registered
