"""Execution guardrails for Vesper — the successor to the deleted orders.py.

This is the only module that decides whether an OrderProposal is allowed to
reach a broker's place-order call. It exists because the sidecar->Vesper
migration (commit de60d51) deleted orders.py's notional cap, ticket handshake,
and kill switch without rebuilding them, leaving vesper/nodes/executor.py free
to submit live orders with none of those guards. See
docs/CODE_SWEEP_2026-08-28.md for the full history.

Design, carried over from orders.py because the reasoning still applies:

1. Preview, then place with a ticket. preview() runs the guards and stages a
   single-use, short-TTL ticket carrying a hash of the exact payload. place()
   re-hashes the payload it's given and refuses to fire if it doesn't match
   the ticket — so what got approved is byte-for-byte what gets sent.
2. Guards run here, not in the caller. Notional cap, quantity cap, optional
   symbol allowlist, and a live-buying-power fraction check all run
   server-side, independent of whichever broker adapter ends up placing the
   order.
3. VESPER_TRADING is the kill switch, checked before anything else. Unlike
   the old sidecar (which defaulted this on), it defaults OFF here: this is a
   freshly rebuilt path that hasn't been exercised against a live account, so
   the safe state is "does nothing" until a human deliberately opts in.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from typing import Any, Callable, Optional

TICKET_TTL_SEC = 120.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _load_guard_config() -> dict:
    """Read env-based guard config fresh (not at import time) so tests and
    callers that mutate os.environ don't have to fight module-level caching."""
    allow = os.environ.get("VESPER_SYMBOL_ALLOWLIST", "").strip()
    return {
        "trading_enabled": _env_bool("VESPER_TRADING", False),
        "max_notional": _env_float("VESPER_MAX_NOTIONAL", 2500.0),
        "max_quantity": _env_float("VESPER_MAX_QUANTITY", 25.0),
        # Fraction of live buying power a single order may consume. 1.0 disables it.
        "max_bp_fraction": _env_float("VESPER_MAX_BP_FRACTION", 1.0),
        "symbol_allowlist": {s.strip().upper() for s in allow.split(",") if s.strip()},
    }


class GuardError(ValueError):
    """Rejected before anything reached the broker."""


class TradingDisabled(GuardError):
    pass


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class Ticket:
    __slots__ = ("id", "proposal_id", "digest", "created_at", "used")

    def __init__(self, proposal_id: str, payload: Any) -> None:
        self.id = uuid.uuid4().hex
        self.proposal_id = proposal_id
        self.digest = _digest(payload)
        self.created_at = time.time()
        self.used = False

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > TICKET_TTL_SEC


class ExecutionGuard:
    """Stateful ticket store + guard checks for one process's worth of orders.

    One instance is expected to live for the lifetime of the running Vesper
    process (created once, not per-node) so tickets survive between the
    preview and place steps of a single execution pass.
    """

    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()

    def _reap(self) -> None:
        for tid in [t for t, tk in self._tickets.items() if tk.expired]:
            self._tickets.pop(tid, None)

    def _validate(
        self,
        payload: dict,
        config: dict,
        live_buying_power: Optional[float],
    ) -> None:
        from vesper.halt import is_halted
        halted, halt_info = is_halted()
        if halted and halt_info:
            raise TradingDisabled(
                f"Vesper is HALTED via emergency switch: '{halt_info.get('reason')}' "
                f"(triggered by {halt_info.get('halted_by')} at {halt_info.get('halted_at')})"
            )

        if not config["trading_enabled"]:
            raise TradingDisabled(
                "Vesper live trading is disabled (VESPER_TRADING is not set to 1). "
                "This is the intended default until Module 0 has been exercised "
                "against a live account — see docs/CODE_SWEEP_2026-08-28.md."
            )

        symbol = str(payload.get("symbol", "")).upper()
        allowlist = config["symbol_allowlist"]
        if allowlist and symbol not in allowlist:
            raise GuardError(f"{symbol} is not in VESPER_SYMBOL_ALLOWLIST")

        quantity = float(payload.get("quantity") or 0)
        if quantity <= 0:
            raise GuardError("quantity must be > 0")
        if quantity > config["max_quantity"]:
            raise GuardError(
                f"quantity {quantity:g} exceeds VESPER_MAX_QUANTITY ({config['max_quantity']:g})"
            )

        limit_price = float(payload.get("limit_price") or 0)
        is_option = str(payload.get("asset_type", "")).upper() == "OPTION"
        multiplier = 100.0 if is_option else 1.0

        is_opening_short_option = (
            is_option
            and str(payload.get("side", "")).upper() == "SELL"
            and not payload.get("is_closing", False)
        )
        if is_opening_short_option:
            # Selling an option to OPEN a new position (a cash-secured put, a
            # covered call) commits capital equal to the strike, not the
            # premium collected -- limit_price here is a few dollars of
            # premium while the real obligation on assignment is
            # strike*100*quantity, which can be 10-100x larger. Using
            # limit_price for this branch would let a short option sail past
            # VESPER_MAX_NOTIONAL while the guard believes it's looking at a
            # tiny order. Require the strike explicitly rather than silently
            # under-counting the risk. (A SELL that's *closing* an existing
            # long position — monitor.py's exit cascade — sets
            # payload["is_closing"]=True and skips this: for a close, the
            # market value limit_price*100*qty is the correct figure, not
            # the strike.)
            strike = payload.get("strike")
            if strike is None:
                raise GuardError(
                    "cannot assess risk of a short option (side=SELL, asset_type=OPTION) "
                    "without a strike price in the payload — refusing rather than "
                    "under-counting notional from the premium alone"
                )
            notional = quantity * float(strike) * multiplier
        else:
            notional = quantity * limit_price * multiplier

        if notional and notional > config["max_notional"]:
            raise GuardError(
                f"order notional ~${notional:,.2f} exceeds VESPER_MAX_NOTIONAL "
                f"(${config['max_notional']:,.2f}). Raise the cap deliberately if you mean it."
            )

        if config["max_bp_fraction"] < 1.0 and notional and payload.get("side", "").upper() in ("BUY", "SHORT"):
            bp = live_buying_power or 0.0
            if bp and notional > bp * config["max_bp_fraction"]:
                raise GuardError(
                    f"order notional ~${notional:,.2f} exceeds "
                    f"{config['max_bp_fraction']:.0%} of ${bp:,.2f} buying power"
                )

    def preview(
        self,
        proposal_id: str,
        payload: dict,
        live_buying_power: Optional[float] = None,
    ) -> Ticket:
        """Run the guards and stage a single-use ticket. Raises GuardError /
        TradingDisabled if the order should not proceed."""
        with self._lock:
            self._reap()
            config = _load_guard_config()
            self._validate(payload, config, live_buying_power)
            ticket = Ticket(proposal_id, payload)
            self._tickets[ticket.id] = ticket
            return ticket

    def place(self, ticket_id: str, payload: dict, place_fn: Callable[[], Any]) -> Any:
        """Redeem a ticket and, only if it matches, call place_fn() to actually
        submit the order. place_fn is broker-specific and supplied by the
        caller so this module stays broker-agnostic."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise GuardError(f"unknown or already-reaped ticket {ticket_id}")
            if ticket.used:
                raise GuardError(f"ticket {ticket_id} was already used")
            if ticket.expired:
                self._tickets.pop(ticket_id, None)
                raise GuardError(f"ticket {ticket_id} expired (TTL {TICKET_TTL_SEC:g}s)")
            if _digest(payload) != ticket.digest:
                raise GuardError(
                    f"payload for ticket {ticket_id} does not match what was previewed "
                    "— refusing to place a different order than the one that was approved"
                )
            ticket.used = True

        return place_fn()


# Process-lifetime singleton. executor_node imports this rather than
# constructing its own ExecutionGuard, so a ticket staged by one node
# invocation is still redeemable by the next.
guard = ExecutionGuard()
