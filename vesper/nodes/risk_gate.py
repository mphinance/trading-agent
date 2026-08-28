"""Deterministic Risk Gate Node."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.account import FALLBACK_EQUITY, fetch_live_equity
from vesper.state import TradingState, OrderProposal
from vesper.risk import RiskEnforcer

logger = logging.getLogger(__name__)


async def risk_gate_node(state: TradingState) -> Dict[str, Any]:
    """Applies strict deterministic safety checks before presenting to human."""
    logger.info("-> [RiskGateNode] Enforcing zero-loss budget & risk limits...")

    proposals = state.get("proposals", [])
    valid_proposals: List[OrderProposal] = []
    rejected_proposals: List[OrderProposal] = []
    audit_notes = []

    # One live-equity read per risk-gate pass, not per proposal — wb.py already
    # caches/rate-limits the underlying account-balance call, but there's no
    # reason to hit it more than once here.
    account_equity = await asyncio.to_thread(fetch_live_equity) if proposals else FALLBACK_EQUITY

    from vesper.llm import audit_proposal_risk, is_llm_enabled
    regime = state.get("regime")
    regime_posture = regime.posture if regime else "NEUTRAL"

    for prop in proposals:
        is_valid, err = RiskEnforcer.validate_proposal(prop, account_equity=account_equity)
        if is_valid:
            # Qualitative LLM Red-Team Audit (if OpenRouter active)
            if is_llm_enabled():
                try:
                    audit_res = await audit_proposal_risk(
                        proposal_dict=prop.model_dump(),
                        regime_posture=regime_posture,
                    )
                    if not audit_res.get("passed", True) or audit_res.get("recommendation") == "REJECT":
                        err = f"LLM Risk Audit Rejection: {', '.join(audit_res.get('concerns', ['Unfavorable risk profile']))}"
                        logger.warning(f"REJECTED by LLM Risk Gate: {prop.id} - {err}")
                        prop.rejection_reason = err
                        rejected_proposals.append(prop)
                        audit_notes.append(f"LLM REJECTED {prop.id}: {err}")
                        continue
                    elif audit_res.get("recommendation") == "REDUCE_SIZE" and prop.quantity > 1:
                        prop.quantity = max(1, prop.quantity // 2)
                        prop.estimated_cost = round(prop.quantity * prop.limit_price, 2)
                        prop.max_risk = round(prop.max_risk * 0.5, 2)
                        audit_notes.append(f"LLM Risk Gate halved position size for {prop.id} (new qty: {prop.quantity})")
                except Exception as e:
                    logger.debug("LLM risk audit skipped: %s", e)

            valid_proposals.append(prop)
            audit_notes.append(f"PASSED Risk Gate: {prop.id} ({prop.ticker} {prop.side} {prop.quantity}x)")
        else:
            logger.warning(f"REJECTED by Risk Gate: {prop.id} - {err}")
            prop.rejection_reason = err
            rejected_proposals.append(prop)
            audit_notes.append(f"REJECTED {prop.id}: {err}")

    audit_entry = {
        "node": "risk_gate_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed_count": len(valid_proposals),
        "rejected_count": len(rejected_proposals),
        "notes": audit_notes,
    }

    return {
        "proposals": valid_proposals,
        "rejected_proposals": rejected_proposals,
        "needs_human_approval": len(valid_proposals) > 0,
        "audit_trail": [audit_entry],
    }
