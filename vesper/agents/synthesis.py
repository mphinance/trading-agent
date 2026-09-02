"""Debate & Synthesis Supervisor (Portfolio Manager)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from vesper.state import TradingState, WorkerReport, OrderProposal, TechnicalAudit, OptionAudit

logger = logging.getLogger(__name__)


class DebateSynthesisSupervisor:
    """Portfolio Manager & Chief Investment Officer node resolving agent conflicts and synthesizing proposals."""

    def __init__(self):
        # Default weights when no historical attribution weights exist
        self.default_weights = {
            "technical_agent": 0.35,
            "flow_agent": 0.25,
            "gamma_agent": 0.20,
            "fundamental_agent": 0.20,
        }

    def resolve_ticker_debate(
        self,
        ticker: str,
        reports: List[WorkerReport],
        weights: Optional[Dict[str, float]] = None,
    ) -> Tuple[str, float, str, Dict[str, Any]]:
        """Debates worker reports for a ticker and synthesizes final direction & conviction.

        Returns:
            Tuple of (direction, synthesized_confidence, synthesized_thesis, debate_record)
        """
        active_weights = weights or self.default_weights
        total_weight = sum(active_weights.get(r.agent_name, 0.20) for r in reports) or 1.0

        weighted_score = 0.0
        bullish_reasons = []
        bearish_reasons = []
        neutral_reasons = []

        for r in reports:
            w = active_weights.get(r.agent_name, 0.20) / total_weight
            signed_conf = r.confidence_score if r.direction == "BULLISH" else (-r.confidence_score if r.direction == "BEARISH" else 0.0)
            weighted_score += signed_conf * w

            if r.direction == "BULLISH":
                bullish_reasons.append(f"{r.agent_name} ({r.confidence_score:.0f}%): {r.thesis_summary}")
            elif r.direction == "BEARISH":
                bearish_reasons.append(f"{r.agent_name} ({r.confidence_score:.0f}%): {r.thesis_summary}")
            else:
                neutral_reasons.append(f"{r.agent_name} ({r.confidence_score:.0f}%): {r.thesis_summary}")

        has_conflict = len(bullish_reasons) > 0 and len(bearish_reasons) > 0

        # Arbitration logic
        if weighted_score >= 15.0:
            direction = "BULLISH"
            final_conf = min(95.0, 50.0 + (weighted_score / 2.0))
        elif weighted_score <= -15.0:
            direction = "BEARISH"
            final_conf = min(95.0, 50.0 + (abs(weighted_score) / 2.0))
        else:
            direction = "NEUTRAL"
            final_conf = 50.0

        # Build debate transcript
        debate_record = {
            "ticker": ticker,
            "has_conflict": has_conflict,
            "weighted_score": round(weighted_score, 2),
            "final_direction": direction,
            "final_confidence": round(final_conf, 1),
            "bullish_arguments": bullish_reasons,
            "bearish_arguments": bearish_reasons,
            "neutral_arguments": neutral_reasons,
            "resolution": (
                f"Resolved via multi-agent debate: {direction} with {final_conf:.1f}% conviction."
                + (" (Conflict arbitrated across time horizons)" if has_conflict else "")
            ),
        }

        # Build synthesized thesis
        primary_reasons = bullish_reasons if direction == "BULLISH" else (bearish_reasons if direction == "BEARISH" else neutral_reasons)
        thesis_body = " | ".join(primary_reasons[:2]) if primary_reasons else "Balanced multi-agent assessment"
        synthesized_thesis = f"[Swarm Consensus: {direction} {final_conf:.0f}%] {thesis_body}"
        if has_conflict:
            synthesized_thesis += f" [Arbitrated: {len(bullish_reasons)} Bullish vs {len(bearish_reasons)} Bearish]"

        return direction, round(final_conf, 1), synthesized_thesis, debate_record

    def synthesize_all(
        self,
        state: TradingState,
    ) -> Tuple[List[Dict[str, Any]], List[OrderProposal]]:
        """Run debate synthesis across all candidates and enrich/filter proposals."""
        worker_reports_dict = state.get("worker_reports", {})
        existing_proposals = state.get("proposals", [])
        custom_weights = state.get("agent_conviction_weights")

        debate_transcripts: List[Dict[str, Any]] = []
        enriched_proposals: List[OrderProposal] = []

        # Map existing proposals by ticker for quick enrichment
        proposals_by_ticker = {p.ticker: p for p in existing_proposals}

        for ticker, reports in worker_reports_dict.items():
            if not reports:
                continue

            direction, confidence, thesis, transcript = self.resolve_ticker_debate(
                ticker=ticker,
                reports=reports,
                weights=custom_weights,
            )
            debate_transcripts.append(transcript)

            if ticker in proposals_by_ticker:
                proposal = proposals_by_ticker[ticker].model_copy(deep=True)
                # Enrich thesis with multi-agent consensus
                proposal.thesis = thesis
                proposal.thesis_source = "vesper/swarm_debate"
                
                # Check for stop invalidation levels across worker reports
                all_stops = [lvl for r in reports for lvl in r.invalidation_levels if lvl > 0]
                if all_stops and proposal.stop_loss is None:
                    proposal.stop_loss = max(all_stops) if proposal.side == "BUY" else min(all_stops)

                enriched_proposals.append(proposal)

        # Include any proposals that didn't have reports untouched
        for p in existing_proposals:
            if p.ticker not in worker_reports_dict:
                enriched_proposals.append(p)

        return debate_transcripts, enriched_proposals
