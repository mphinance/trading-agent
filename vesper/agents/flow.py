"""Institutional & Options Flow Specialist Agent."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from vesper.agents.base import BaseSpecialistAgent
from vesper.state import TradingState, WorkerReport
from vesper.flow_classifier import classify_flow

logger = logging.getLogger(__name__)


class InstitutionalFlowAgent(BaseSpecialistAgent):
    """Specialist agent tracking institutional positioning, whale prints, and options order flow."""

    name: str = "flow_agent"
    specialty: str = "institutional_and_options_flow"

    async def analyze(self, ticker: str, state: TradingState) -> WorkerReport:
        candidates = state.get("candidates", [])
        matched_candidate = next((c for c in candidates if c.ticker == ticker), None)

        catalysts: List[str] = []
        invalidation_levels: List[float] = []
        score = 50.0
        time_horizon = "SWING_SHORT"

        candidate_data = matched_candidate.data if matched_candidate else {}
        source = matched_candidate.source if matched_candidate else "MANUAL"

        # Check candidate source markers
        if source == "0DTE_FLOW":
            time_horizon = "INTRADAY_0DTE"
            score += 20.0
            catalysts.append("High-velocity 0DTE options surge detected")
        elif source == "WHALE_CONVERGENCE":
            score += 25.0
            catalysts.append("Multi-institution 13F / whale conviction cluster")
        elif source == "SQUEEZE":
            score += 15.0
            catalysts.append("Short squeeze flow & high borrow fee pressure")

        # Parse flow records if present in candidate payload
        flow_records = candidate_data.get("unusual_activity", [])
        if isinstance(flow_records, list) and flow_records:
            directional_calls = 0
            directional_puts = 0
            hedges = 0

            for rec in flow_records:
                if not isinstance(rec, dict):
                    continue
                size = float(rec.get("size", rec.get("volume", 0)))
                oi = float(rec.get("open_interest", 1))
                iv = float(rec.get("iv", 0.30))
                opt_type = str(rec.get("type", rec.get("option_type", "CALL"))).upper()
                sent = rec.get("sentiment")
                
                classification = classify_flow(
                    trade_size=size,
                    open_interest=oi,
                    iv=iv,
                    option_type=opt_type,
                    sentiment=sent,
                )

                if classification == "DIRECTIONAL":
                    if "CALL" in opt_type or sent == "Bullish":
                        directional_calls += 1
                    else:
                        directional_puts += 1
                elif classification == "HEDGE":
                    hedges += 1

            if directional_calls > directional_puts:
                score += 15.0
                catalysts.append(f"Institutional Net Bullish Flow ({directional_calls} directional calls vs {directional_puts} puts)")
            elif directional_puts > directional_calls:
                score -= 20.0
                catalysts.append(f"Institutional Net Bearish Flow ({directional_puts} directional puts vs {directional_calls} calls)")

            if hedges > 0:
                catalysts.append(f"Detected {hedges} institutional protective hedge print(s)")

        # Determine directional posture
        if score >= 65.0:
            direction = "BULLISH"
            suggested_playbook = "0dte_flow" if time_horizon == "INTRADAY_0DTE" else "institutional_convergence"
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
            thesis_summary=f"Flow Analysis: {direction} institutional alignment with {len(catalysts)} flow trigger(s).",
            suggested_playbook=suggested_playbook,
            data={"source": source, "catalysts_count": len(catalysts)},
        )
