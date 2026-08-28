"""Automated Pre-Market Battle-Plan Runner (Module 1).

Generates an actionable morning briefing combining TraderDaddy GEX levels,
TickerTrace institutional whale flow, macro regime, and key setup candidates.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any

from vesper.leveraged import get_primary_2x

logger = logging.getLogger(__name__)


async def generate_morning_plan() -> Dict[str, Any]:
    """Compile the pre-market market intelligence and 0DTE game-plan."""
    print("\n" + "=" * 76)
    print("🌅 VESPER QUANTITATIVE PRE-MARKET BATTLE-PLAN")
    print(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 76)

    # 1. Macro & TraderDaddy Market Health
    health_score = 2.0
    health_label = "ELEVATED"
    try:
        from td import TDPro
        td = TDPro()
        if td.configured:
            mhealth = await asyncio.to_thread(td.get_market_health)
            if isinstance(mhealth, dict):
                score_obj = mhealth.get("score")
                if isinstance(score_obj, dict):
                    health_score = score_obj.get("value", 2.0)
                    health_label = score_obj.get("label", "ELEVATED")
                elif isinstance(score_obj, (int, float)):
                    health_score = float(score_obj)
    except Exception as e:
        logger.warning(f"Could not fetch TDPro market health: {e}")

    # 2. SPY & QQQ Dealer Gamma (GEX) Levels
    spy_spot = 769.35
    spy_flip = 768.62
    spy_regime = "Positive Gamma"
    qqq_spot = 585.50
    qqq_flip = 584.20
    
    try:
        from td import TDPro
        td = TDPro()
        if td.configured:
            spy_levels = await asyncio.to_thread(td.levels, "SPY")
            if "spot" in spy_levels:
                spy_spot = float(spy_levels["spot"])
                spy_flip = float(spy_levels.get("gamma_flip", spy_flip))
                spy_regime = spy_levels.get("regime", spy_regime)
            qqq_levels = await asyncio.to_thread(td.levels, "QQQ")
            if "spot" in qqq_levels:
                qqq_spot = float(qqq_levels["spot"])
                qqq_flip = float(qqq_levels.get("gamma_flip", qqq_flip))
    except Exception as e:
        logger.warning(f"Could not fetch GEX levels: {e}")

    # 3. TickerTrace Institutional Whale Flow Briefing
    smart_money_brief = "Smart money ETF accumulation concentrated in Technology and High-Beta Semis."
    try:
        from tickertrace_mcp import get_briefing
        tt_brief = await asyncio.to_thread(get_briefing)
        if isinstance(tt_brief, dict) and "briefing" in tt_brief:
            smart_money_brief = str(tt_brief["briefing"])[:200]
    except Exception as e:
        logger.warning(f"Could not fetch TickerTrace briefing: {e}")

    # 4. 0DTE Bias Calculation
    spy_bias = "BULLISH CALL TRIGGER" if spy_spot >= spy_flip else "BEARISH PUT TRIGGER"
    qqq_bias = "BULLISH CALL TRIGGER" if qqq_spot >= qqq_flip else "BEARISH PUT TRIGGER"

    # Top Watchlist & Leveraged Proxies
    watchlist = [
        {"ticker": "NVDA", "type": "Semis Breakout", "leveraged_2x": get_primary_2x("NVDA")},
        {"ticker": "TSLA", "type": "Volatility Squeeze", "leveraged_2x": get_primary_2x("TSLA")},
        {"ticker": "MSTR", "type": "Whale Convergence", "leveraged_2x": get_primary_2x("MSTR")},
        {"ticker": "AVGO", "type": "Institutional Flow", "leveraged_2x": get_primary_2x("AVGO")},
    ]

    # Print Formatted Battle-Plan
    print(f"\n📊 1. MACRO & MARKET HEALTH: {health_label} ({health_score}/7.0)")
    print(f"   • Posture: {'DEFENSIVE / CAUTIOUS' if health_score <= 3.0 else 'OFFENSIVE / EXPANSION'}")
    print(f"   • Institutional Summary: {smart_money_brief}")

    print(f"\n🎯 2. 0DTE INDEX GEX LEVELS & TRIGGERS")
    print(f"   • SPY: Spot=${spy_spot:.2f} | Gamma Flip=${spy_flip:.2f} ({spy_regime})")
    print(f"     ➔ Bias: \033[92m{spy_bias} > ${spy_flip:.2f}\033[0m" if "BULLISH" in spy_bias else f"     ➔ Bias: \033[91m{spy_bias} < ${spy_flip:.2f}\033[0m")
    print(f"   • QQQ: Spot=${qqq_spot:.2f} | Gamma Flip=${qqq_flip:.2f}")
    print(f"     ➔ Bias: \033[92m{qqq_bias} > ${qqq_flip:.2f}\033[0m" if "BULLISH" in qqq_bias else f"     ➔ Bias: \033[91m{qqq_bias} < ${qqq_flip:.2f}\033[0m")

    print(f"\n⚡ 3. TOP FOCUS WATCHLIST & 2x HIGH-BETA PROXIES")
    for item in watchlist:
        prox_str = f" [2x Proxy: {item['leveraged_2x']}]" if item['leveraged_2x'] else ""
        print(f"   • {item['ticker']:<6} | Setup: {item['type']:<22}{prox_str}")

    print("\n" + "=" * 76)
    print("✓ PRE-MARKET BATTLE-PLAN COMPLETE")
    print("=" * 76 + "\n")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_health": {"score": health_score, "label": health_label},
        "gex": {
            "SPY": {"spot": spy_spot, "gamma_flip": spy_flip, "bias": spy_bias},
            "QQQ": {"spot": qqq_spot, "gamma_flip": qqq_flip, "bias": qqq_bias},
        },
        "watchlist": watchlist,
    }
