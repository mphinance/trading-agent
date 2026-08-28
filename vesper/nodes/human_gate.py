"""Human-In-The-Loop Approval Gate Node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Any

from vesper.state import TradingState
from langgraph.types import interrupt

logger = logging.getLogger(__name__)


def human_gate_node(state: TradingState) -> Dict[str, Any]:
    """Halts execution to present structured trade proposals for explicit approval."""
    proposals = state.get("proposals", [])
    if not proposals:
        return {
            "human_decision": "NO_PROPOSALS",
            "audit_trail": [{
                "node": "human_gate_node",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "decision": "NO_PROPOSALS",
            }]
        }

    logger.info(f"-> [HumanGateNode] {len(proposals)} proposal(s) awaiting approval.")
    
    # Check if human decision already provided (e.g. via CLI or resume)
    decision = state.get("human_decision")
    if not decision:
        # Generate summary payload for human inspection
        summary_cards = []
        for p in proposals:
            summary_cards.append({
                "id": p.id,
                "ticker": p.ticker,
                "asset": p.asset_type,
                "action": f"{p.side} {p.quantity} @ ${p.limit_price:.2f}",
                "stop_loss": f"${p.stop_loss:.2f}" if p.stop_loss else "N/A",
                "target": f"${p.profit_target:.2f}" if p.profit_target else "N/A",
                "estimated_cost": f"${p.estimated_cost:,.2f}",
                "max_risk": f"${p.max_risk:,.2f}",
            })
        
        # In interactive graph execution, interrupt with the card details
        try:
            decision = interrupt({
                "type": "ORDER_CONFIRMATION_REQUIRED",
                "proposals": summary_cards,
                "prompt": "Approve this order execution? [APPROVE / REJECT / ABORT]",
            })
        except Exception:
            # Fallback when running outside interrupt handler
            decision = "AUTO_DRY_RUN" if state.get("mode") == "dry_run" else "PENDING"

    # Apply approval state
    for p in proposals:
        p.approved = (decision == "APPROVE" or decision == "AUTO_DRY_RUN")

    audit_entry = {
        "node": "human_gate_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision": str(decision),
        "proposals_approved": sum(1 for p in proposals if p.approved),
    }

    return {
        "human_decision": str(decision),
        "audit_trail": [audit_entry],
    }
