"""Debate & Synthesis Portfolio Manager Node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from vesper.state import TradingState
from vesper.agents.synthesis import DebateSynthesisSupervisor
from vesper.agents.risk_adversary import AdversarialRiskAgent

logger = logging.getLogger(__name__)


async def synthesis_node(state: TradingState) -> Dict[str, Any]:
    """Debates worker reports across candidates, resolves conflicts, and enriches proposals."""
    logger.info("-> [SynthesisNode] Running multi-agent debate & thesis synthesis...")

    supervisor = DebateSynthesisSupervisor()
    adversary = AdversarialRiskAgent()

    debate_transcripts, enriched_proposals = supervisor.synthesize_all(state)

    audit_entries: List[Dict[str, Any]] = []
    reflection_notes: List[str] = []

    # Run adversarial red-teaming on enriched proposals
    red_team_results = []
    for proposal in enriched_proposals:
        critique = adversary.red_team_proposal(proposal=proposal, state=state)
        red_team_results.append(critique)
        if critique["verdict"] == "WARNING":
            reflection_notes.append(f"Adversarial Warning for {proposal.ticker}: {critique['counter_thesis']}")

    audit_entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": "DEBATE_AND_SYNTHESIS",
        "debates_conducted": len(debate_transcripts),
        "proposals_evaluated": len(enriched_proposals),
        "red_team_warnings": sum(1 for r in red_team_results if r["verdict"] == "WARNING"),
    })

    logger.info(
        f"-> [SynthesisNode] Completed {len(debate_transcripts)} debate(s) with {len(enriched_proposals)} synthesized proposal(s)."
    )

    return {
        "debate_transcripts": debate_transcripts,
        "proposals": enriched_proposals,
        "reflection_notes": reflection_notes,
        "audit_trail": audit_entries,
    }
