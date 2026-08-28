"""Deterministic Risk Gate Node."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.state import TradingState, OrderProposal
from vesper.risk import RiskEnforcer

logger = logging.getLogger(__name__)

_FALLBACK_EQUITY = 10000.0


def _fetch_live_equity() -> float:
    """Blocking: constructs its own Webull client and reads NLV. Runs on a
    worker thread (see asyncio.to_thread call below) since this SDK is
    synchronous and this node is async."""
    try:
        from wb import Webull

        wb = Webull()
        if not wb.configured:
            return _FALLBACK_EQUITY
        nlv = wb.portfolio()["totals"]["nlv"]
        return nlv or _FALLBACK_EQUITY
    except Exception as e:
        logger.warning(f"Could not fetch live account equity, falling back to ${_FALLBACK_EQUITY:,.0f}: {e}")
        return _FALLBACK_EQUITY


async def risk_gate_node(state: TradingState) -> Dict[str, Any]:
    """Applies strict deterministic safety checks before presenting to human."""
    logger.info("-> [RiskGateNode] Enforcing zero-loss budget & risk limits...")

    proposals = state.get("proposals", [])
    valid_proposals: List[OrderProposal] = []
    audit_notes = []

    # One live-equity read per risk-gate pass, not per proposal — wb.py already
    # caches/rate-limits the underlying account-balance call, but there's no
    # reason to hit it more than once here.
    account_equity = await asyncio.to_thread(_fetch_live_equity) if proposals else _FALLBACK_EQUITY

    for prop in proposals:
        is_valid, err = RiskEnforcer.validate_proposal(prop, account_equity=account_equity)
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
