"""Human-In-The-Loop Approval Gate Node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Any

from vesper.state import TradingState
from langgraph.types import interrupt

logger = logging.getLogger(__name__)


async def human_gate_node(state: TradingState) -> Dict[str, Any]:
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

    # Register in ApprovalRegistry for inbound webhook / bot resolution
    from vesper.bot.inbound import approval_registry
    session_id = state.get("session_id", "vesper-session")
    for p in proposals:
        approval_registry.register_pending(
            proposal_id=p.id,
            session_id=session_id,
            details={"ticker": p.ticker, "side": p.side, "limit_price": p.limit_price, "quantity": p.quantity},
        )

    # Broadcast to configured channels (Telegram, Discord, Webhooks)
    from vesper.bot.manager import channel_manager
    if channel_manager.active_channels:
        for p in proposals:
            # Pass `state` through so the card can show the before/after
            # allocation-bucket diff risk_gate_node computed one node
            # earlier (account_equity/live_buying_power/capital_snapshot),
            # and so p.thesis (now populated at draft time -- see
            # playbooks.py) actually reaches the card instead of a
            # never-passed empty string.
            await channel_manager.broadcast_proposal(p, state=state)

    # Check if human decision already provided (e.g. via CLI, inbound callback, or resume)
    decision = state.get("human_decision")
    if not decision:
        # Check if any proposal was already resolved via inbound webhook/bot
        for p in proposals:
            inbound_dec = approval_registry.get_decision(p.id)
            if inbound_dec:
                decision = inbound_dec.get("decision")
                break
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
                "target": f"${p.target_price:.2f}" if p.target_price else "N/A",
                "estimated_cost": f"${(p.limit_price or 0.0) * p.quantity:,.2f}",
                "max_risk": f"${p.max_risk_usd:,.2f}",
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
