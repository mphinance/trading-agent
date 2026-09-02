"""Gamma Structure & 0DTE Specialist Agent."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from vesper.agents.base import BaseSpecialistAgent
from vesper.state import TradingState, WorkerReport, OptionAudit

logger = logging.getLogger(__name__)


class GammaStructureAgent(BaseSpecialistAgent):
    """Specialist agent analyzing dealer gamma positioning (GEX), Apex levels, and 0DTE options structure."""

    name: str = "gamma_agent"
    specialty: str = "gamma_and_0dte_structure"

    async def analyze(self, ticker: str, state: TradingState) -> WorkerReport:
        regime = state.get("regime")
        options_audits: Dict[str, OptionAudit] = state.get("options_audits", {})
        opt_audit = options_audits.get(ticker)

        catalysts: List[str] = []
        invalidation_levels: List[float] = []
        score = 50.0
        time_horizon = "INTRADAY_0DTE"

        # 1. Evaluate market gamma posture from Regime
        if regime:
            if regime.spy_gex_regime == "POSITIVE":
                catalysts.append("Positive Dealer Gamma regime (volatility dampening / mean-reverting)")
                score += 10.0
            elif regime.spy_gex_regime == "NEGATIVE":
                catalysts.append("Negative Dealer Gamma regime (volatility accelerator / expansion)")
                time_horizon = "INTRADAY_0DTE"
                score += 15.0

            if regime.spy_gamma_flip and regime.spy_spot:
                if regime.spy_spot > regime.spy_gamma_flip:
                    catalysts.append(f"SPY trading above Gamma Flip level ({regime.spy_gamma_flip:.2f})")
                    score += 10.0
                else:
                    catalysts.append(f"SPY trading below Gamma Flip level ({regime.spy_gamma_flip:.2f})")
                    score -= 15.0

        # 2. Check VoPR grade if option audit exists
        if opt_audit:
            if opt_audit.vopr_grade in ("A+", "A", "B+"):
                score += 20.0
                catalysts.append(f"High VoPR™ Pricing Grade ({opt_audit.vopr_grade}) on {opt_audit.strike} {opt_audit.option_type}")
                if opt_audit.strike:
                    invalidation_levels.append(opt_audit.strike)
            elif opt_audit.vopr_grade in ("D", "F"):
                score -= 20.0
                catalysts.append(f"Unfavorable VoPR™ Pricing Grade ({opt_audit.vopr_grade})")

        # 3. Check for Apex levels if candidate data holds them
        candidates = state.get("candidates", [])
        matched = next((c for c in candidates if c.ticker == ticker), None)
        if matched and matched.data.get("apex_levels"):
            apex = matched.data["apex_levels"]
            catalysts.append(f"Apex Key Levels: S1={apex.get('s1')}, R1={apex.get('r1')}")
            if apex.get("s1"):
                try:
                    invalidation_levels.append(float(apex["s1"]))
                except (ValueError, TypeError):
                    pass

        # Direction and confidence
        if score >= 65.0:
            direction = "BULLISH"
            suggested_playbook = "0dte_flow" if time_horizon == "INTRADAY_0DTE" else "adx_iv_router"
        elif score <= 35.0:
            direction = "BEARISH"
            suggested_playbook = "volatility_harvester"
        else:
            direction = "NEUTRAL"
            suggested_playbook = "collar_following"

        confidence = max(20.0, min(95.0, score if direction == "BULLISH" else (100.0 - score if direction == "BEARISH" else 50.0)))

        return WorkerReport(
            agent_name=self.name,
            ticker=ticker,
            direction=direction,
            confidence_score=round(confidence, 1),
            time_horizon=time_horizon,
            key_catalysts=catalysts,
            invalidation_levels=invalidation_levels,
            thesis_summary=f"Gamma Analysis: {direction} setup with {len(catalysts)} gamma trigger(s).",
            suggested_playbook=suggested_playbook,
            data={"time_horizon": time_horizon, "catalysts": catalysts},
        )
