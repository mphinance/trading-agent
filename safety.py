"""Order-safety pipeline.

Ported from mphinance/TraderDaddy-Desktop (src-tauri/src/commands/orders.rs),
which had a more rigorous design than sidecar's first pass:

  preview → mints a single-use token bound to the exact params, 60s TTL
  arm     → session flag, defaults OFF on every boot
  place   → requires armed + a live token whose params still match
  journal → the order is written to disk BEFORE we report success

The bug this fixes: previously a successful preview simply enabled the Place
button. Changing the ticket afterwards left it enabled, so you could place an
order that was never previewed. Binding the token to the params closes that.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

PREVIEW_TTL_SECS = 60.0
JOURNAL = Path(__file__).resolve().parent / "orders.jsonl"

# Fields that must be identical between preview and place.
BOUND_FIELDS = ("symbol", "side", "quantity", "limit_price")

# SEC Rule 612: equities quote in $0.01 increments at/above $1.00, $0.0001 below.
TICK_ABOVE_DOLLAR = Decimal("0.01")
TICK_BELOW_DOLLAR = Decimal("0.0001")


class BadPrice(Exception):
    """Limit price isn't on a valid tick."""


def tick_for(price: Decimal) -> Decimal:
    return TICK_ABOVE_DOLLAR if price >= 1 else TICK_BELOW_DOLLAR


def round_to_tick(price: float | str | Decimal) -> Decimal:
    """Normalise a limit price onto the instrument's tick.

    Two layers, borrowed from nautilus (`make_price` then reject excess
    precision): round at construction, then verify. Without this, a sub-penny
    price passes our guard and Webull decides what happens to it.
    """
    d = Decimal(str(price))
    return d.quantize(tick_for(d), rounding=ROUND_HALF_UP)


def check_tick(price: float | str | Decimal) -> Decimal:
    """Reject a price that isn't already on-tick, rather than silently moving it.

    Silently rounding a price the user typed is its own hazard — freqtrade notes
    rounding direction is side-dependent for stops. Safer to refuse and let them
    re-enter than to trade a price they didn't choose.
    """
    d = Decimal(str(price))
    if d <= 0:
        raise BadPrice("limit price must be greater than zero")
    if d != round_to_tick(d):
        raise BadPrice(f"{d} is not on a valid tick — use {tick_for(d)} increments "
                       f"(nearest: {round_to_tick(d)})")
    return d


def fmt_price(price: Decimal | float) -> str:
    """Format for the wire. `str(1e-05)` yields '1e-05'; Webull wants '0.0001'."""
    return format(Decimal(str(price)), "f")


class PreviewExpired(Exception):
    """Token unknown, already used, or older than the TTL."""


class PreviewMismatch(Exception):
    """Token is live but the order no longer matches what was previewed."""


class NotArmed(Exception):
    """Trading is disarmed for this session."""


class ArmFlag:
    """Session-scoped arm flag. Defaults OFF on every process start.

    Deliberately not persisted: a restart should never come back armed, which
    matters most when the app is left running unattended or on stream.
    """

    def __init__(self) -> None:
        self._armed = False
        self._lock = threading.Lock()
        self._armed_at: float | None = None

    @property
    def armed(self) -> bool:
        return self._armed

    def set(self, armed: bool) -> bool:
        with self._lock:
            self._armed = bool(armed)
            self._armed_at = time.time() if armed else None
        return self._armed

    def state(self) -> dict:
        return {"armed": self._armed,
                "armed_for_secs": round(time.time() - self._armed_at) if self._armed_at else None}


class PreviewVault:
    """Single-use tokens minted by preview and consumed by place."""

    def __init__(self, ttl: float = PREVIEW_TTL_SECS) -> None:
        self._ttl = ttl
        self._entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def mint(self, params: dict, client_order_id: str) -> str:
        """Mint a token bound to these params AND to one client_order_id.

        The client_order_id is minted once at preview and reused at place. If a
        place times out ambiguously and the user previews again, the retry
        carries a *different* id — so binding it here means a genuine retry gets
        rejected by the broker as a duplicate instead of doubling the position.
        """
        token = uuid.uuid4().hex
        now = time.monotonic()
        with self._lock:
            # Drop expired entries opportunistically so the map can't grow.
            self._entries = {k: v for k, v in self._entries.items() if v["expires_at"] > now}
            self._entries[token] = {
                "params": {f: params.get(f) for f in BOUND_FIELDS},
                "client_order_id": client_order_id,
                "expires_at": now + self._ttl,
            }
        return token

    def consume(self, token: str, params: dict) -> str:
        """Pop and validate; returns the bound client_order_id.

        Raises PreviewExpired / PreviewMismatch.
        """
        with self._lock:
            entry = self._entries.pop(token, None)   # pop first — single use
        if entry is None:
            raise PreviewExpired("preview token unknown or already used")
        if entry["expires_at"] < time.monotonic():
            raise PreviewExpired(f"preview older than {int(self._ttl)}s — preview again")
        incoming = {f: params.get(f) for f in BOUND_FIELDS}
        if entry["params"] != incoming:
            raise PreviewMismatch(
                f"order changed since preview: previewed {entry['params']}, got {incoming}"
            )
        return entry["client_order_id"]

    def ttl(self) -> float:
        return self._ttl


def journal(record: dict) -> None:
    """Append-only order log, written BEFORE success is reported.

    If this write fails the order may still have been placed at Webull — the
    caller surfaces that rather than swallowing it, so you go check the app.
    """
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(record) + "\n")


def confirm_token(account_number: str) -> str:
    """Last 4 of the account number — confirming proves you read which account.

    Better than a fixed word: a constant like "PLACE" can be muscle-memoried,
    and it never makes you look at the account you're about to trade.
    """
    return (account_number or "")[-4:].upper()
