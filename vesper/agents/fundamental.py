"""Fundamental & Catalysts Specialist Agent."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from vesper.agents.base import BaseSpecialistAgent
from vesper.state import TradingState, WorkerReport

logger = logging.getLogger(__name__)


class FundamentalAgent(BaseSpecialistAgent):
    """Specialist agent analyzing SEC filings, earnings surprises, valuation, and balance sheet health."""

    name: str = "fundamental_agent"
    specialty: str = "fundamental_analysis"

    async def analyze(self, ticker: str, state: TradingState) -> WorkerReport:
        catalysts: List[str] = []
        invalidation_levels: List[float] = []
        score = 50.0
        time_horizon = "POSITION_LONG"

        # 1. Check candidate data for pre-screened fundamental catalysts
        candidates = state.get("candidates", [])
        matched = next((c for c in candidates if c.ticker == ticker), None)
        if matched and matched.catalyst:
            catalysts.append(f"Pre-screened Catalyst: {matched.catalyst}")
            score += 15.0

        # 2. Attempt asynchronous non-blocking SEC EDGAR filings lookup
        try:
            import edgar
            recent_filings = await asyncio.to_thread(edgar.filings, ticker=ticker, months=3)
            if hasattr(recent_filings, "empty") and not recent_filings.empty:
                form_types = set(recent_filings.get("form", []))
                if "8-K" in form_types:
                    catalysts.append("Recent 8-K material event filing in last 90 days")
                    score += 5.0
                if "10-Q" in form_types or "10-K" in form_types:
                    catalysts.append("Active quarterly/annual reporting compliance")
                    score += 5.0
        except Exception as e:
            logger.debug(f"[{self.name}] EDGAR filings lookup skipped for {ticker}: {e}")

        # 3. Evaluate PEAD / Earnings harvest signals if candidate source matches
        if matched and "EARNINGS" in matched.source.upper():
            time_horizon = "SWING_SHORT"
            score += 20.0
            catalysts.append("Post-Earnings Announcement Drift (PEAD) momentum catalyst")

        # Direction and confidence determination
        if score >= 65.0:
            direction = "BULLISH"
            suggested_playbook = "earnings_vega_harvest" if "EARNINGS" in (matched.source if matched else "") else "thega"
        elif score <= 35.0:
            direction = "BEARISH"
            suggested_playbook = "volatility_harvester"
        else:
            direction = "NEUTRAL"
            suggested_playbook = "collar_following"

        confidence = max(25.0, min(90.0, score if direction == "BULLISH" else (100.0 - score if direction == "BEARISH" else 50.0)))

        return WorkerReport(
            agent_name=self.name,
            ticker=ticker,
            direction=direction,
            confidence_score=round(confidence, 1),
            time_horizon=time_horizon,
            key_catalysts=catalysts,
            invalidation_levels=invalidation_levels,
            thesis_summary=f"Fundamentals: {direction} thesis based on {len(catalysts)} fundamental catalyst(s).",
            suggested_playbook=suggested_playbook,
            data={"catalysts": catalysts},
        )
