"""Draft proposal MCP tool.

M8-15, M8-16, M8-17, M8-18.
Runs the exact same deterministic sizing and risk validation path as the Vesper graph,
registers a PENDING proposal in ApprovalRegistry, and broadcasts an interactive card
across configured approval channels.

Strict exposure boundary:
Can neither approve nor place an order.
Never references guard.preview, guard.place, resume(), or ApprovalRegistry.submit_decision().
"""

from __future__ import annotations

import difflib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from core.approval_registry import approval_registry
from core.audit_chain import append_entry
from vesper.risk import RiskEnforcer
from vesper.state import OrderProposal

logger = logging.getLogger(__name__)

# Common ticker mappings for voice mis-transcriptions
_COMMON_MAPPINGS: dict[str, str] = {
    "in video": "NVDA",
    "invidia": "NVDA",
    "nvidia": "NVDA",
    "apple": "AAPL",
    "spy": "SPY",
    "spies": "SPY",
    "cues": "QQQ",
    "triple q": "QQQ",
    "qqq": "QQQ",
    "tesla": "TSLA",
    "tezla": "TSLA",
    "amazon": "AMZN",
    "google": "GOOGL",
    "meta": "META",
    "microsoft": "MSFT",
}

_TRACKED_UNIVERSE = [
    "SPY", "QQQ", "IWM", "NVDA", "AAPL", "MSFT", "AMZN",
    "GOOGL", "GOOG", "META", "TSLA", "AMD", "COIN", "PLTR",
]


def resolve_draft_symbol(query: str) -> tuple[str | None, list[str] | None]:
    """Resolve a spoken symbol query to a ticker, handling mis-transcriptions and ambiguities.

    M8-17.
    """
    cleaned = (query or "").strip().lower()
    if not cleaned:
        return None, None

    # 1. Known exact mappings
    if cleaned in _COMMON_MAPPINGS:
        return _COMMON_MAPPINGS[cleaned], None

    # 2. Check uppercase candidate
    upper_query = cleaned.upper()
    if upper_query in _TRACKED_UNIVERSE:
        return upper_query, None

    # 3. Check for ambiguity against universe
    matches = difflib.get_close_matches(upper_query, _TRACKED_UNIVERSE, n=3, cutoff=0.6)
    if len(matches) > 1 and abs(
        difflib.SequenceMatcher(None, upper_query, matches[0]).ratio()
        - difflib.SequenceMatcher(None, upper_query, matches[1]).ratio()
    ) < 0.10:
        return None, matches
    if len(matches) == 1:
        return matches[0], None

    # 4. If clean alphanumeric 1-5 letters, accept as raw ticker
    if re.fullmatch(r"[A-Za-z]{1,5}", cleaned):
        return upper_query, None

    return None, None


async def draft_proposal(
    symbol_query: str,
    side: str = "BUY",
    entry_price: float | None = None,
    stop_loss: float | None = None,
    profit_target: float | None = None,
    thesis: str = "Spoken order proposal via MCP",
    account_equity: float | None = None,
) -> dict[str, Any]:
    """Draft an order proposal using deterministic sizing and risk-gate validation.

    Registers PENDING in ApprovalRegistry and broadcasts interactive card to channels.
    Never executes, never fills, never approves.
    """
    # 1. Resolve ticker symbol (M8-17)
    ticker, candidates = resolve_draft_symbol(symbol_query)
    if not ticker:
        if candidates:
            return {
                "available": False,
                "reason": "ambiguous_symbol",
                "message": f"Ambiguous symbol query '{symbol_query}'. Did you mean one of: {', '.join(candidates)}?",
                "candidates": candidates,
            }
        return {
            "available": False,
            "reason": "invalid_symbol",
            "message": f"Could not resolve valid ticker symbol from '{symbol_query}'.",
        }

    side = (side or "BUY").strip().upper()
    if side not in ("BUY", "SELL"):
        return {"available": False, "reason": "invalid_side", "message": "Side must be BUY or SELL"}

    # 2. Check deduplication within pending proposals (M8-17)
    for p in approval_registry.list_pending():
        details = p.get("details", {})
        if (
            details.get("ticker") == ticker
            and details.get("side") == side
            and p.get("status") == "PENDING"
        ):
            return {
                "available": True,
                "deduplicated": True,
                "proposal_id": p.get("proposal_id"),
                "ticker": ticker,
                "side": side,
                "quantity": details.get("quantity", 1),
                "entry": details.get("limit_price", details.get("entry")),
                "stop": details.get("stop"),
                "target": details.get("target"),
                "speakable_summary": (
                    f"A pending {side} proposal for {ticker} already exists ({p.get('proposal_id')}). "
                    f"Returned existing proposal instead of queuing duplicate."
                ),
            }

    # 3. Determine price, stop, target
    if entry_price is None or entry_price <= 0:
        try:
            from core.wb import Webull
            from core.md import Market

            wb = Webull()
            md = Market(wb)
            snap = md.snapshot(ticker)
            entry_price = float(snap[ticker].get("last") or snap[ticker].get("close") or 100.0)
        except Exception:
            entry_price = 100.0

    if stop_loss is None or stop_loss <= 0:
        stop_loss = round(entry_price * 0.98, 2) if side == "BUY" else round(entry_price * 1.02, 2)

    if profit_target is None or profit_target <= 0:
        risk_dist = abs(entry_price - stop_loss)
        profit_target = round(entry_price + (risk_dist * 2.0), 2) if side == "BUY" else round(entry_price - (risk_dist * 2.0), 2)

    if account_equity is None or account_equity <= 0:
        try:
            from core.wb import Webull

            wb = Webull()
            if wb.configured:
                port = wb.portfolio()
                account_equity = float(port.get("totals", {}).get("nlv") or 10000.0)
            else:
                account_equity = 10000.0
        except Exception:
            account_equity = 10000.0

    # 4. Sizing via pure RiskEnforcer path (M8-15)
    shares, total_cost, total_risk = RiskEnforcer.calculate_equity_size(
        account_equity=account_equity,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        target_price=profit_target,
    )
    if shares <= 0:
        return {
            "available": False,
            "reason": "risk_gate_rejected",
            "message": f"Position sizing calculated 0 shares (stop too close or invalid risk parameters for {ticker}).",
        }

    proposal_id = f"prop-{uuid.uuid4().hex[:8]}"
    prop = OrderProposal(
        id=proposal_id,
        ticker=ticker,
        asset_type="EQUITY",
        side=side,
        order_type="LIMIT",
        quantity=shares,
        limit_price=entry_price,
        stop_loss=stop_loss,
        profit_target=profit_target,
        thesis=thesis,
        estimated_cost=total_cost,
        max_risk=total_risk,
    )

    # 5. Deterministic risk validation (M8-15)
    is_valid, err = RiskEnforcer.validate_proposal(prop, account_equity=account_equity)
    if not is_valid:
        return {
            "available": False,
            "reason": "risk_gate_rejected",
            "message": f"Proposal rejected by RiskEnforcer: {err}",
        }

    # Capital allocation bucket validation
    is_valid_buckets, bucket_err = RiskEnforcer.check_capital_allocation_buckets(
        prop,
        open_long_option_count=0,
        wheel_stock_notional=0.0,
        account_equity=account_equity,
    )
    if not is_valid_buckets:
        return {
            "available": False,
            "reason": "risk_gate_rejected",
            "message": f"Proposal rejected by capital allocation bucket: {bucket_err}",
        }

    # 6. Register as PENDING in approval registry (M8-15)
    registered = approval_registry.register_pending(
        proposal_id=proposal_id,
        session_id="mcp-draft",
        details={
            "ticker": ticker,
            "side": side,
            "quantity": shares,
            "limit_price": entry_price,
            "stop": stop_loss,
            "target": profit_target,
            "risk": total_risk,
            "thesis": thesis,
            "drafted_by": "MCP",
        },
    )

    # 7. Record drafting in audit trail (M8-16)
    try:
        append_entry(
            session_id="mcp-draft",
            node="mcp_draft_proposal",
            entry={
                "proposal_id": proposal_id,
                "ticker": ticker,
                "side": side,
                "quantity": shares,
                "limit_price": entry_price,
                "stop_loss": stop_loss,
                "profit_target": profit_target,
                "drafted_by": "MCP",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        logger.warning(f"Failed to record draft in audit ledger: {e}")

    # 8. Broadcast to configured channels via channel_manager (M8-16)
    try:
        from vesper.bot.manager import channel_manager

        if channel_manager.active_channels:
            await channel_manager.broadcast_proposal(prop, thesis=thesis)
    except Exception as e:
        logger.warning(f"Channel broadcast failed for proposal {proposal_id}: {e}")

    speakable_summary = (
        f"Drafted {side} {shares} shares of {ticker} at {entry_price:.2f}, "
        f"stop {stop_loss:.2f}, target {profit_target:.2f}. "
        f"Requires button approval to execute."
    )

    return {
        "available": True,
        "deduplicated": False,
        "proposal_id": proposal_id,
        "ticker": ticker,
        "side": side,
        "quantity": shares,
        "entry": entry_price,
        "stop": stop_loss,
        "target": profit_target,
        "risk": total_risk,
        "speakable_summary": speakable_summary,
        "status": "PENDING",
    }


def register_drafting_tools(mcp: Any) -> list[str]:
    """Register draft_proposal tool with safe-write scope requirement."""
    from fastmcp.server.auth import require_scopes

    safe_write_auth = require_scopes("safe-write")

    @mcp.tool(auth=safe_write_auth)
    async def draft_proposal_tool(
        symbol_query: str,
        side: str = "BUY",
        entry_price: float | None = None,
        stop_loss: float | None = None,
        profit_target: float | None = None,
        thesis: str = "Spoken order proposal via MCP",
    ) -> dict[str, Any]:
        """Draft an order proposal using deterministic sizing and risk-gate validation.

        Registers PENDING in the approval registry and broadcasts an interactive card to
        configured channels. Never executes or places orders.
        """
        return await draft_proposal(
            symbol_query=symbol_query,
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            profit_target=profit_target,
            thesis=thesis,
        )

    return ["draft_proposal_tool"]
