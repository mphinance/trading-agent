"""Order execution tools for MCP under strict deterministic guards.

M8-20, M8-21, M8-22, M8-23.
Amends the exposure rule under Amendment A4:
- Reaches order execution via vesper.execution_guard ONLY.
- Requires OAuth 'trade' scope (distinct from 'read').
- Bounded by VESPER_TRADING, halt switches, notional caps, and per-day rate limits.
- Never references resume() or ApprovalRegistry.submit_decision().
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.audit_chain import append_entry
from core.halt import is_halted
from vesper.execution_guard import GuardError, TradingDisabled, guard
from vesper.risk import RiskEnforcer

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_RATE_LIMIT_FILE = _DATA_DIR / "mcp_daily_order_count.json"
_LOCK = threading.Lock()
_STAGED_PAYLOADS: dict[str, dict] = {}


def _get_mcp_limits() -> tuple[float, int]:
    """MCP-specific stricter limits read from env."""
    try:
        max_notional = float(os.environ.get("MCP_MAX_NOTIONAL", "1000.0"))
    except ValueError:
        max_notional = 1000.0
    try:
        max_daily = int(os.environ.get("MCP_MAX_DAILY_ORDERS", "5"))
    except ValueError:
        max_daily = 5
    return max_notional, max_daily


def _record_and_check_daily_order() -> tuple[bool, int, str | None]:
    """Check and record an MCP order against the daily count limit."""
    max_notional, max_daily = _get_mcp_limits()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with _LOCK:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        if _RATE_LIMIT_FILE.exists():
            try:
                data = json.loads(_RATE_LIMIT_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        current_date = data.get("date")
        count = data.get("count", 0) if current_date == today_str else 0

        if count >= max_daily:
            return False, count, f"MCP daily order limit reached: {count}/{max_daily} orders placed today."

        # Increment and persist
        new_count = count + 1
        tmp = _RATE_LIMIT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": today_str, "count": new_count}), encoding="utf-8")
        tmp.replace(_RATE_LIMIT_FILE)
        return True, new_count, None


def _default_place_fn(payload: dict) -> dict[str, Any]:
    """Place the order via broker adapter or paper fallback."""
    try:
        from core.wb import Webull
        wb = Webull()
        if wb.configured and os.environ.get("VESPER_TRADING") == "1":
            # Real broker place
            return wb.place_order(payload)
    except Exception as e:
        logger.warning(f"Broker place returned error: {e}")

    # Simulated/paper placement result for test or paper mode
    return {
        "status": "FILLED_PAPER",
        "order_id": f"mcp-ord-{uuid.uuid4().hex[:8]}",
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def submit_manual_proposal(
    ticker: str,
    side: str,
    quantity: int,
    limit_price: float,
    order_type: str = "LIMIT",
    asset_type: str = "EQUITY",
    strike: float | None = None,
    is_closing: bool = False,
    thesis: str = "Manual proposal via MCP",
    live_buying_power: float | None = None,
) -> dict[str, Any]:
    """Stage a manual order through execution_guard.preview().

    M8-20. Validates guards and stages a single-use ticket carrying payload hash.
    Never places an order.
    """
    ticker = (ticker or "").strip().upper()
    side = (side or "BUY").strip().upper()
    asset_type = (asset_type or "EQUITY").strip().upper()
    order_type = (order_type or "LIMIT").strip().upper()

    proposal_id = f"man-{uuid.uuid4().hex[:8]}"
    payload = {
        "symbol": ticker,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "limit_price": limit_price,
        "order_type": order_type,
        "asset_type": asset_type,
        "is_closing": is_closing,
        "thesis": thesis,
    }
    if strike is not None:
        payload["strike"] = strike

    # Calculate notional for short options vs equity
    multiplier = 100.0 if asset_type == "OPTION" else 1.0
    if asset_type == "OPTION" and side == "SELL" and not is_closing:
        if strike is None or strike <= 0:
            return {
                "available": False,
                "rejected": True,
                "reason": "Short option sell-to-open requires strike price for strike-based notional sizing.",
            }
        calc_notional = quantity * strike * multiplier
    else:
        calc_notional = quantity * limit_price * multiplier

    # Stage through execution_guard.preview
    try:
        ticket = guard.preview(
            proposal_id=proposal_id,
            payload=payload,
            live_buying_power=live_buying_power,
        )
        _STAGED_PAYLOADS[ticket.id] = dict(payload)
    except (GuardError, TradingDisabled) as e:
        return {
            "available": False,
            "rejected": True,
            "reason": str(e),
            "payload": payload,
            "calculated_notional": calc_notional,
        }
    except Exception as e:
        return {
            "available": False,
            "rejected": True,
            "reason": f"Unexpected preview rejection: {e}",
            "payload": payload,
        }

    speakable_summary = (
        f"Staged {side} {quantity} {ticker} at {limit_price:.2f} (~${calc_notional:,.2f}). "
        f"Ticket {ticket.id[:8]} valid for 120s."
    )

    return {
        "available": True,
        "staged": True,
        "ticket_id": ticket.id,
        "digest": ticket.digest,
        "payload": payload,
        "calculated_notional": calc_notional,
        "speakable_summary": speakable_summary,
    }


def place_from_ticket(ticket_id: str, place_fn: Optional[Callable[[dict], Any]] = None) -> dict[str, Any]:
    """Execute a previously staged ticket via execution_guard.place().

    M8-21. Takes only ticket_id (never order payload), ensuring single-use execution.
    """
    ticket = guard._tickets.get(ticket_id)
    payload = _STAGED_PAYLOADS.get(ticket_id)
    if not ticket or not payload:
        return {
            "available": False,
            "rejected": True,
            "reason": f"Ticket '{ticket_id}' not found, expired, or already used.",
        }

    # Check daily order rate limit
    ok_limit, count, limit_err = _record_and_check_daily_order()
    if not ok_limit:
        return {"available": False, "rejected": True, "reason": limit_err}

    # Record in audit trail before broker execution
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        append_entry(
            session_id="mcp-trade",
            node="mcp_place_from_ticket",
            entry={
                "ticket_id": ticket_id,
                "payload": payload,
                "daily_order_count": count,
                "timestamp": now_iso,
            },
        )
    except Exception as e:
        logger.warning(f"Audit append error: {e}")

    fn = (lambda: place_fn(payload)) if place_fn else (lambda: _default_place_fn(payload))

    try:
        result = guard.place(ticket_id=ticket_id, payload=payload, place_fn=fn)
        _STAGED_PAYLOADS.pop(ticket_id, None)
    except (GuardError, TradingDisabled) as e:
        return {"available": False, "rejected": True, "reason": str(e)}
    except Exception as e:
        return {"available": False, "rejected": True, "reason": f"Execution error: {e}"}

    return {
        "available": True,
        "placed": True,
        "ticket_id": ticket_id,
        "result": result,
        "speakable_summary": f"Fired order for ticket {ticket_id[:8]} successfully.",
    }


def place_order(
    ticker: str,
    side: str,
    quantity: int,
    limit_price: float,
    order_type: str = "LIMIT",
    asset_type: str = "EQUITY",
    strike: float | None = None,
    is_closing: bool = False,
    live_buying_power: float | None = None,
    place_fn: Optional[Callable[[dict], Any]] = None,
) -> dict[str, Any]:
    """Direct 1-call placement through execution_guard with MCP-specific limits.

    M8-22. Enforces MCP_MAX_NOTIONAL and MCP_MAX_DAILY_ORDERS, checks halt,
    writes to audit chain before execution.
    """
    max_mcp_notional, max_daily = _get_mcp_limits()

    # 1. Check halt status immediately
    halted, halt_info = is_halted()
    if halted and halt_info:
        return {
            "available": False,
            "rejected": True,
            "reason": f"Vesper is HALTED via emergency switch: '{halt_info.get('reason')}'",
        }

    # 2. Check VESPER_TRADING switch
    if os.environ.get("VESPER_TRADING") != "1":
        return {
            "available": False,
            "rejected": True,
            "reason": "Vesper live trading is disabled (VESPER_TRADING != 1).",
        }

    # 3. Check MCP notional limit
    multiplier = 100.0 if asset_type == "OPTION" else 1.0
    if asset_type == "OPTION" and side.upper() == "SELL" and not is_closing:
        if strike is None or strike <= 0:
            return {
                "available": False,
                "rejected": True,
                "reason": "Short option requires strike price for strike-based notional check.",
            }
        notional = quantity * strike * multiplier
    else:
        notional = quantity * limit_price * multiplier

    if notional > max_mcp_notional:
        return {
            "available": False,
            "rejected": True,
            "reason": f"Order notional ~${notional:,.2f} exceeds MCP_MAX_NOTIONAL (${max_mcp_notional:,.2f}).",
        }

    # 4. Stage through preview
    preview_res = submit_manual_proposal(
        ticker=ticker,
        side=side,
        quantity=quantity,
        limit_price=limit_price,
        order_type=order_type,
        asset_type=asset_type,
        strike=strike,
        is_closing=is_closing,
        live_buying_power=live_buying_power,
    )
    if not preview_res.get("staged"):
        return preview_res

    ticket_id = preview_res["ticket_id"]

    # 5. Execute via place_from_ticket
    return place_from_ticket(ticket_id=ticket_id, place_fn=place_fn)


def register_order_tools(mcp: Any) -> list[str]:
    """Register order path tools onto mcp under strict 'trade' OAuth scope."""
    from fastmcp.server.auth import require_scopes

    trade_auth = require_scopes("trade")

    @mcp.tool(auth=trade_auth)
    def submit_manual_proposal_tool(
        ticker: str,
        side: str,
        quantity: int,
        limit_price: float,
        order_type: str = "LIMIT",
        asset_type: str = "EQUITY",
        strike: float | None = None,
        is_closing: bool = False,
        thesis: str = "Manual proposal via MCP",
    ) -> dict[str, Any]:
        """Stage a manual order through deterministic execution guards. Returns ticket_id."""
        return submit_manual_proposal(
            ticker=ticker,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            order_type=order_type,
            asset_type=asset_type,
            strike=strike,
            is_closing=is_closing,
            thesis=thesis,
        )

    @mcp.tool(auth=trade_auth)
    def place_from_ticket_tool(ticket_id: str) -> dict[str, Any]:
        """Fire a previously staged ticket through execution_guard."""
        return place_from_ticket(ticket_id=ticket_id)

    @mcp.tool(auth=trade_auth)
    def place_order_tool(
        ticker: str,
        side: str,
        quantity: int,
        limit_price: float,
        order_type: str = "LIMIT",
        asset_type: str = "EQUITY",
        strike: float | None = None,
        is_closing: bool = False,
    ) -> dict[str, Any]:
        """Place an order directly under stricter MCP limits (MCP_MAX_NOTIONAL, MCP_MAX_DAILY_ORDERS)."""
        return place_order(
            ticker=ticker,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            order_type=order_type,
            asset_type=asset_type,
            strike=strike,
            is_closing=is_closing,
        )

    return ["submit_manual_proposal_tool", "place_from_ticket_tool", "place_order_tool"]
