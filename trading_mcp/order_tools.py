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
    """MCP-specific stricter limits read from env.

    `MCP_MAX_NOTIONAL` is an ABSOLUTE CEILING, not the operative cap -- see
    `_effective_notional_cap()`, which scales it down to the size of the
    actual book. A flat dollar ceiling on a small account is not a cap: at
    the $1000 default it was 2.5x the buying power of the live account it
    was pointed at, so every order it could ever have blocked would have
    been rejected by the broker first anyway.
    """
    try:
        max_notional = float(os.environ.get("MCP_MAX_NOTIONAL", "1000.0"))
    except ValueError:
        max_notional = 1000.0
    try:
        max_daily = int(os.environ.get("MCP_MAX_DAILY_ORDERS", "5"))
    except ValueError:
        max_daily = 5
    return max_notional, max_daily


def _get_notional_pct() -> float:
    """Fraction of net liquidation value one MCP order may commit."""
    try:
        pct = float(os.environ.get("MCP_MAX_NOTIONAL_PCT", "0.25"))
    except ValueError:
        pct = 0.25
    # A pct <= 0 would refuse everything and a pct > 1 would be no cap at
    # all; both are far more likely to be a typo than an intention.
    if not (0.0 < pct <= 1.0):
        pct = 0.25
    return pct


def _effective_notional_cap() -> tuple[float, dict[str, Any]]:
    """The operative per-order notional cap: min(absolute ceiling, pct x NLV).

    Portfolio-aware by construction. Returns `(cap, detail)`; a cap of 0.0
    means REFUSE -- this fails CLOSED when net liquidation value cannot be
    read, deliberately and for the same reason `alerts.resolve_level()`
    returns None rather than a remembered number: a cap computed from an
    unknown book is not a cap, and silently falling back to the flat ceiling
    is exactly the failure being fixed here.

    A `stale` account read is accepted. It is bounded by the absolute
    ceiling either way, and a slightly old NLV is a far better basis than
    none; the detail dict reports staleness so the caller can say so.
    """
    ceiling, _ = _get_mcp_limits()
    pct = _get_notional_pct()

    try:
        from trading_mcp.vesper_tools import fetch_account_state

        state = fetch_account_state()
    except Exception as e:  # pragma: no cover - defensive
        return 0.0, {"reason": f"Account state unreadable ({e}).", "ceiling": ceiling}

    if not state.get("available"):
        return 0.0, {
            "reason": (
                "Cannot size a portfolio-aware cap: account state unavailable "
                f"({state.get('fetch_error') or 'no detail'}). Refusing rather "
                "than falling back to a flat ceiling."
            ),
            "ceiling": ceiling,
        }

    nlv = state.get("net_liquidation")
    if not isinstance(nlv, (int, float)) or nlv <= 0:
        return 0.0, {
            "reason": f"Cannot size a portfolio-aware cap: net_liquidation={nlv!r}.",
            "ceiling": ceiling,
        }

    portfolio_cap = float(nlv) * pct
    cap = min(ceiling, portfolio_cap)
    return cap, {
        "net_liquidation": float(nlv),
        "pct": pct,
        "portfolio_cap": portfolio_cap,
        "ceiling": ceiling,
        "binding": "portfolio" if portfolio_cap < ceiling else "ceiling",
        "stale": bool(state.get("stale")),
    }


def _validate_close(
    ticker: str, side: str, quantity: int, asset_type: str
) -> tuple[bool, str | None]:
    """Is this order genuinely reducing an existing position?

    `is_closing` is a caller-asserted flag. Nothing in `execution_guard` or
    `risk.py` validates it -- which was harmless while the only caller was
    `monitor.py`'s exit cascade, where the flag merely selected market value
    over strike for the notional of a short option. It stops being harmless
    the moment a close SKIPS the notional cap on tools an LLM can call, since
    `is_closing=True` would then be a one-flag bypass of the entire cap.

    So a close must be checked against the live book: a position in that
    symbol must exist, the order must be on the opposite side of it, and it
    must not exceed the quantity held. Anything else is not a close, whatever
    it claims.
    """
    try:
        from trading_mcp.vesper_tools import fetch_account_state

        state = fetch_account_state()
    except Exception as e:  # pragma: no cover - defensive
        return False, f"Cannot verify this is a close: account unreadable ({e})."

    if not state.get("available"):
        return False, (
            "Cannot verify this is a close: account state unavailable "
            f"({state.get('fetch_error') or 'no detail'})."
        )

    ticker = ticker.strip().upper()
    want_option = asset_type.strip().upper() == "OPTION"
    held = 0.0
    for pos in state.get("positions") or []:
        sym = str(pos.get("symbol", "")).strip().upper()
        is_option_pos = str(pos.get("instrument_type", "")).upper() == "OPTION"
        if is_option_pos != want_option:
            continue
        # An equity position is keyed by the ticker itself; an option
        # position carries a contract symbol that begins with the underlying.
        if sym == ticker or (want_option and sym.startswith(ticker)):
            try:
                held += float(pos.get("quantity") or 0)
            except (TypeError, ValueError):
                continue

    if held == 0:
        return False, (
            f"Refusing: is_closing=True but no {asset_type.upper()} position "
            f"in {ticker} is held. A close must reduce something you own."
        )

    side_u = side.strip().upper()
    if held > 0 and side_u != "SELL":
        return False, (
            f"Refusing: is_closing=True with side={side_u}, but the {ticker} "
            f"position is LONG {held:g}. Closing a long is a SELL."
        )
    if held < 0 and side_u != "BUY":
        return False, (
            f"Refusing: is_closing=True with side={side_u}, but the {ticker} "
            f"position is SHORT {abs(held):g}. Closing a short is a BUY."
        )
    if quantity > abs(held):
        return False, (
            f"Refusing: is_closing=True for {quantity} {ticker} but only "
            f"{abs(held):g} held. Closing more than you own opens a position "
            f"in the other direction."
        )
    return True, None


def _notional_for(
    quantity: int,
    limit_price: float,
    asset_type: str,
    side: str,
    strike: float | None,
    is_closing: bool,
) -> tuple[float | None, str | None]:
    """Notional for one order, or `(None, reason)` if it cannot be computed.

    A SELL-to-open option is sized off the STRIKE, not the premium -- the
    same rule `vesper/execution_guard.py` enforces, and for the same reason:
    a cash-secured put commits `strike x 100 x qty` on assignment, so
    reading `limit_price` here lets a five-figure risk past a four-figure
    cap. Kept in one helper so the two call sites cannot drift apart.
    """
    multiplier = 100.0 if asset_type.upper() == "OPTION" else 1.0
    if asset_type.upper() == "OPTION" and side.upper() == "SELL" and not is_closing:
        if strike is None or strike <= 0:
            return None, (
                "Short option sell-to-open requires strike price for "
                "strike-based notional sizing."
            )
        return quantity * strike * multiplier, None
    return quantity * limit_price * multiplier, None


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

    calc_notional, notional_err = _notional_for(
        quantity, limit_price, asset_type, side, strike, is_closing
    )
    if calc_notional is None:
        return {"available": False, "rejected": True, "reason": notional_err}

    # The MCP notional cap is enforced HERE, at the single staging chokepoint,
    # and not only in `place_order`. Every path that can reach the broker goes
    # through this function: `place_order` calls it, and `place_from_ticket`
    # can only fire a ticket that it staged. Checking the cap solely in
    # `place_order` -- as this module did originally -- left the two-step tool
    # path (`submit_manual_proposal_tool` -> `place_from_ticket_tool`) bounded
    # by nothing but the guard's own, much larger, absolute cap. Pinned by
    # `test_two_step_path_cannot_bypass_mcp_notional_cap`; don't move it back.
    exempt = False
    if is_closing:
        exempt, close_err = _validate_close(ticker, side, quantity, asset_type)
        if not exempt:
            return {
                "available": False,
                "rejected": True,
                "reason": close_err,
                "payload": payload,
                "calculated_notional": calc_notional,
            }

    if not exempt:
        cap, detail = _effective_notional_cap()
        if cap <= 0:
            return {
                "available": False,
                "rejected": True,
                "reason": detail.get("reason", "Notional cap unavailable."),
                "payload": payload,
                "calculated_notional": calc_notional,
            }
        if calc_notional > cap:
            return {
                "available": False,
                "rejected": True,
                "reason": (
                    f"Order notional ~${calc_notional:,.2f} exceeds the "
                    f"portfolio-aware MCP cap of ${cap:,.2f} "
                    f"({detail.get('pct', 0) * 100:.0f}% of "
                    f"${detail.get('net_liquidation', 0):,.2f} NLV, binding "
                    f"constraint: {detail.get('binding')})."
                ),
                "payload": payload,
                "calculated_notional": calc_notional,
                "cap_detail": detail,
            }

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

    # 3. Check the portfolio-aware MCP notional limit. This is a fast
    #    pre-check for a clean error before anything is staged; the
    #    authoritative enforcement is inside submit_manual_proposal below,
    #    which every order path shares.
    notional, notional_err = _notional_for(
        quantity, limit_price, asset_type, side, strike, is_closing
    )
    if notional is None:
        return {"available": False, "rejected": True, "reason": notional_err}

    exempt = False
    if is_closing:
        exempt, close_err = _validate_close(ticker, side, quantity, asset_type)
        if not exempt:
            return {"available": False, "rejected": True, "reason": close_err}

    if not exempt:
        cap, detail = _effective_notional_cap()
        if cap <= 0:
            return {
                "available": False,
                "rejected": True,
                "reason": detail.get("reason", "Notional cap unavailable."),
            }
        if notional > cap:
            return {
                "available": False,
                "rejected": True,
                "reason": (
                    f"Order notional ~${notional:,.2f} exceeds the "
                    f"portfolio-aware MCP cap of ${cap:,.2f} "
                    f"({detail.get('pct', 0) * 100:.0f}% of "
                    f"${detail.get('net_liquidation', 0):,.2f} NLV)."
                ),
                "cap_detail": detail,
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
