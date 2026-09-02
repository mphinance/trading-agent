"""MCP Prompt templates for Vesper voice co-pilot.

M10-02, M10-03.
Defines copilot_setup(proposal_id) and morning_brief() prompts.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def copilot_setup_text(proposal_id: str) -> str:
    """Generate prompt text for setup watching session (M10-02)."""
    return (
        f"You are the Vesper voice co-pilot monitoring trade setup '{proposal_id}'.\n"
        f"1. Call the `watch_setup` tool with proposal_id='{proposal_id}' on a 30 to 60 second cadence.\n"
        "2. Do not carry thesis text inline; fetch the latest thesis, entry price, and distance via `watch_setup`.\n"
        "3. If `watch_setup` indicates telemetry is unchanged, succinctly confirm nothing has changed.\n"
        "4. Critical safety rule: voice can never approve trades. If the setup triggers, inform the operator: "
        "'Press it yourself, I cannot approve this.'\n"
        "5. Core exposure rule: voice may do anything that cannot increase exposure."
    )


def morning_brief_text() -> str:
    """Compose morning market and account brief (M10-03)."""
    parts = []

    # 1. Bounded Account State (M8-10)
    try:
        from trading_mcp.vesper_tools import get_account_state
        acct = get_account_state()
        if acct.get("available"):
            nlv = acct.get("net_liquidation", 0.0)
            pos_cnt = acct.get("position_count", 0)
            parts.append(f"Account NLV is currently ${nlv:,.2f} with {pos_cnt} open positions.")
        else:
            parts.append("Account state is currently unavailable.")
    except Exception as e:
        logger.debug(f"Account state in brief: {e}")
        parts.append("Account state could not be loaded.")

    # 2. Gamma structure / flip
    try:
        from core.td import TDPro
        td = TDPro()
        if td.configured:
            spy = td.levels("SPY")
            if spy and "spot" in spy and "flip" in spy:
                spot = float(spy["spot"])
                flip = float(spy["flip"])
                regime = "positive gamma" if spot >= flip else "negative gamma"
                parts.append(f"SPY spot is {spot:.2f} relative to the gamma flip at {flip:.2f} ({regime}).")
    except Exception as e:
        logger.debug(f"Gamma structure in brief: {e}")

    # 3. Pending Proposals
    try:
        from core.approval_registry import approval_registry
        pending = approval_registry.list_pending()
        if pending:
            symbols = [p.get("details", {}).get("ticker", "unknown") for p in pending]
            parts.append(f"There are {len(pending)} pending trade proposals awaiting review ({', '.join(symbols)}).")
        else:
            parts.append("No trade proposals are currently pending approval.")
    except Exception as e:
        logger.debug(f"Pending proposals in brief: {e}")

    # 4. Armed Alerts
    try:
        from alerts import AlertStore
        store = AlertStore()
        alerts_list = store.list()
        active_alerts = [a for a in alerts_list if not a.get("triggered")]
        if active_alerts:
            parts.append(f"You have {len(active_alerts)} armed price alerts active.")
    except Exception as e:
        logger.debug(f"Alerts in brief: {e}")

    parts.append("Remember: approve and resume are never reachable here. Buttons move money.")
    return " ".join(parts)


def register_prompts(mcp: Any) -> list[str]:
    """Register copilot_setup and morning_brief FastMCP prompts."""
    @mcp.prompt()
    def copilot_setup(proposal_id: str) -> str:
        """Script for setup-watching session on a cadence."""
        return copilot_setup_text(proposal_id)

    @mcp.prompt()
    def morning_brief() -> str:
        """Short spoken morning briefing of account, gamma structure, pending setups, and alerts."""
        return morning_brief_text()

    return ["copilot_setup", "morning_brief"]
