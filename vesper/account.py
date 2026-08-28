"""Live account figures shared by nodes that need real (not hardcoded) equity.

Both risk_gate_node (the sizing ceiling) and playbooks_node (sizing itself)
need this. Duplicating a hardcoded `10000.0` in both is exactly how the first
one shipped unnoticed — see docs/CODE_SWEEP_2026-08-28.md — so it lives here
once instead of being copy-pasted per node.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

FALLBACK_EQUITY = 10000.0


def fetch_live_equity() -> float:
    """Blocking (constructs its own Webull client and reads NLV) — callers on
    an async node must wrap this in asyncio.to_thread."""
    try:
        from wb import Webull

        wb = Webull()
        if not wb.configured:
            return FALLBACK_EQUITY
        nlv = wb.portfolio()["totals"]["nlv"]
        return nlv or FALLBACK_EQUITY
    except Exception as e:
        logger.warning(f"Could not fetch live account equity, falling back to ${FALLBACK_EQUITY:,.0f}: {e}")
        return FALLBACK_EQUITY
