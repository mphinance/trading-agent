"""Deterministic Risk Gate Node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.state import TradingState, OrderProposal
from vesper.risk import RiskEnforcer

logger = logging.getLogger(__name__)


async def risk_gate_node(state: TradingState) -> Dict[str, Any]:
    """Applies strict deterministic safety checks before presenting to human."""
    logger.info("-> [RiskGateNode] Enforcing zero-loss budget & risk limits...")
    
    proposals = state.get("proposals", [])
    valid_proposals: List[OrderProposal] = []
    audit_notes = []

    for prop in proposals:
        is_valid, err = RiskEnforcer.validate_proposal(prop, account_equity=10000.0)
        if is_valid:
            valid_proposals.append(prop)
            audit_notes.append(f"PASSED Risk Gate: {prop.id} ({prop.ticker} {prop.side} {prop.quantity}x)")
        else:
            logger.warning(f"REJECTED by Risk Gate: {prop.id} - {err}")
            prop.rejection_reason = err
            audit_notes.append(f"REJECTED {prop.id}: {err}")

    audit_entry = {
        "node": "risk_gate_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed_count": len(valid_proposals),
        "notes": audit_notes,
    }

    return {
        "proposals": valid_proposals,
        "needs_human_approval": len(valid_proposals) > 0,
        "audit_trail": [audit_entry],
    }
