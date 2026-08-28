"""Market Regime & Health Analysis Node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Any

from vesper.state import TradingState, MarketRegime
from td import TDPro
from mcp_server.macro_regime import detect_macro_regime
from mcp_server.market_top import detect_market_top

logger = logging.getLogger(__name__)


async def regime_node(state: TradingState) -> Dict[str, Any]:
    """Inspects macro posture, dealer GEX levels, and market health."""
    logger.info("-> [RegimeNode] Assessing market regime & dealer gamma posture...")
    
    notes = []
    posture = "NEUTRAL"
    health_score = 0.0
    health_label = "UNKNOWN"
    spy_spot = None
    spy_gex = None
    spy_flip = None
    dist_days = 0
    macro_name = "NEUTRAL"
    
    # 1. TraderDaddy Pro Market Health & SPY GEX
    try:
        td = TDPro()
        if td.configured:
            health_res = td.call("get_market_health")
            if isinstance(health_res, dict):
                score_info = health_res.get("compositeScore", {})
                health_score = float(score_info.get("value", 0.0))
                health_label = score_info.get("label", "UNKNOWN")
                notes.append(f"TD Market Health: {health_label} ({health_score}/7.0)")
            
            spy_levels = td.levels("SPY")
            if isinstance(spy_levels, dict):
                spy_spot = spy_levels.get("spot")
                spy_gex = spy_levels.get("regime")
                spy_flip = spy_levels.get("flip")
                notes.append(f"SPY Spot: {spy_spot} | GEX: {spy_gex} | Flip: {spy_flip}")
    except Exception as e:
        logger.warning(f"Error querying TraderDaddy in RegimeNode: {e}")

    # 2. Momentum Macro Regime & Distribution Days
    try:
        macro_res = await detect_macro_regime(lookback=90)
        if hasattr(macro_res, "data") and isinstance(macro_res.data, dict):
            macro_name = macro_res.data.get("regime", "NEUTRAL")
            notes.append(f"Macro Regime: {macro_name}")
            
        top_res = await detect_market_top()
        if hasattr(top_res, "data") and isinstance(top_res.data, dict):
            raw_dist = top_res.data.get("distribution_days", 0)
            if isinstance(raw_dist, dict):
                dist_days = max(raw_dist.values()) if raw_dist else 0
            elif isinstance(raw_dist, (int, float)):
                dist_days = int(raw_dist)
            else:
                dist_days = 0
            notes.append(f"Distribution Days: {dist_days}")
    except Exception as e:
        logger.warning(f"Error querying macro tools in RegimeNode: {e}")

    # Synthesize posture
    if dist_days >= 5 or health_label in ("CRITICAL", "LOW") or macro_name in ("BEAR", "RECESSION"):
        posture = "DEFENSIVE"
    elif health_score >= 4.0 and spy_gex == "Positive Gamma" and dist_days < 4:
        posture = "BULLISH"
    elif spy_gex == "Negative Gamma":
        posture = "HIGH_VOLATILITY"
    else:
        posture = "SELECTIVE"

    regime = MarketRegime(
        posture=posture,
        health_score=health_score,
        health_label=health_label,
        spy_spot=spy_spot,
        spy_gex_regime=spy_gex,
        spy_gamma_flip=spy_flip,
        macro_regime=macro_name,
        distribution_days=dist_days,
        notes=notes,
    )
    
    audit_entry = {
        "node": "regime_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "posture": posture,
        "health_score": health_score,
        "notes": notes,
    }
    
    return {
        "regime": regime,
        "audit_trail": [audit_entry],
    }
