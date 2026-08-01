"""Price alerts, including ones whose level IS the dealer-gamma structure.

The reason this exists rather than deferring to a broker: every native alert
system stores a NUMBER. Webull, IBKR and TradingView all freeze whatever level
you typed, and dealer gamma moves daily — the flip that mattered on Monday is a
stale number by Wednesday, still armed, quietly meaningless. An alert here can
instead reference `flip`, `pin`, `wall_above` or `wall_below`, which are
re-resolved from TDPro on every evaluation. That is the whole point; a static
level is supported too, but it is the boring case.

An alert notifies; it never trades. sidecar as a whole *can* trade now
(`orders.py`), but nothing in this module or in `watcher.py` touches that path —
a level being crossed is information, not an instruction. Keep it that way:
auto-execution off a moving dealer level is a different product with a very
different failure mode.

Two properties are load-bearing and easy to destroy by "simplifying" this:

- **A break is a TRANSITION, not a comparison.** Testing `price <= level` fires
  the instant you arm an alert on a level price is already past, which is both
  useless and the most common alert bug there is. Every alert therefore records
  which side price was on and fires only when it crosses. An alert armed on the
  wrong side starts PENDING and waits for price to come back first.

- **A moving level must not fire the alert by itself.** This is the trap unique
  to gamma-aware alerts and it does not exist in any broker's implementation.
  If the flip moves from 745 to 748 while price sits still at 746, price is
  suddenly "below the flip" without having moved a cent. That is not a break.
  So both the previous and the current price are compared against the CURRENT
  level: a crossing then requires PRICE to have moved across it, and a level
  that jumps over a stationary price re-arms instead of firing.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

# Alerts are user data, not secrets, but they still do not belong in the repo
# (rule 2 keeps anything account-shaped out of git by construction).
STATE_DIR = Path(os.environ.get("SIDECAR_STATE_DIR",
                                Path.home() / ".local" / "state" / "webull-sidecar"))
STORE_PATH = STATE_DIR / "alerts.json"

# Level references resolved live from TDPro. Anything else is read as a number.
DYNAMIC_LEVELS = ("flip", "pin", "wall_above", "wall_below")

ABOVE, BELOW = "above", "below"

# An alert is one-shot by default. Repeating alerts need a cooldown or a symbol
# oscillating on the level will notify on every single poll.
DEFAULT_COOLDOWN_SEC = 900.0


class AlertError(ValueError):
    pass


def _now() -> float:
    return time.time()


class Alert(dict):
    """A dict subclass so the store serialises with no conversion layer."""

    @property
    def id(self) -> str:
        return self["id"]


def make_alert(symbol: str, level: str | float, direction: str,
               note: str = "", repeat: bool = False,
               cooldown: float = DEFAULT_COOLDOWN_SEC) -> Alert:
    """Build an alert. Raises AlertError on anything unusable.

    `level` is either a number or one of DYNAMIC_LEVELS. `direction` is the side
    price must END on for the alert to fire, so "break below the 743 wall" is
    direction=below.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol.isalpha() or not 1 <= len(symbol) <= 6:
        raise AlertError("symbol must be 1-6 letters")

    direction = (direction or "").strip().lower()
    if direction not in (ABOVE, BELOW):
        raise AlertError(f"direction must be {ABOVE!r} or {BELOW!r}")

    ref: str | None = None
    static: float | None = None
    if isinstance(level, str) and level.strip().lower() in DYNAMIC_LEVELS:
        ref = level.strip().lower()
    else:
        try:
            static = float(level)
        except (TypeError, ValueError):
            raise AlertError(f"level must be a number or one of {DYNAMIC_LEVELS}")
        if static <= 0:
            raise AlertError("level must be positive")

    return Alert({
        "id": uuid.uuid4().hex[:12],
        "symbol": symbol,
        "level_ref": ref,          # dynamic, re-resolved every evaluation
        "level_static": static,    # or a frozen number
        "direction": direction,
        "note": note[:200],
        "repeat": bool(repeat),
        "cooldown": float(cooldown),
        # "pending" until price is seen on the arming side; then "armed";
        # then "triggered" (one-shot) or back to "armed" (repeat).
        "state": "pending",
        "created": _now(),
        "last_price": None,
        "last_level": None,
        "last_eval": None,
        "triggered_at": None,
        "trigger_count": 0,
    })


def describe(a: dict, level: float | None = None) -> str:
    """One line a human (or a notification) can read."""
    lvl = level if level is not None else a.get("last_level")
    where = a["level_ref"] or f"{a['level_static']:.2f}"
    shown = f"{where}"
    if a["level_ref"] and lvl:
        shown = f"{a['level_ref']} (${lvl:.2f})"
    elif not a["level_ref"]:
        shown = f"${a['level_static']:.2f}"
    return f"{a['symbol']} breaks {a['direction']} {shown}"


def resolve_level(a: dict, levels: dict | None) -> float | None:
    """Current level for an alert. None means "cannot evaluate right now".

    A dynamic alert whose structure is unavailable must return None rather than
    fall back to a remembered number: a stale flip is exactly the failure this
    module exists to avoid, and a silent fallback would reintroduce it.
    """
    if a["level_ref"] is None:
        return a["level_static"]
    if not levels or levels.get("error"):
        return None

    ref = a["level_ref"]
    if ref == "flip":
        return levels.get("flip")
    if ref == "pin":
        return levels.get("pin")

    spot = levels.get("spot")
    walls = levels.get("walls") or []
    if not spot or not walls:
        return None
    if ref == "wall_above":
        above = [w["strike"] for w in walls if w["strike"] > spot]
        return min(above) if above else None
    if ref == "wall_below":
        below = [w["strike"] for w in walls if w["strike"] < spot]
        return max(below) if below else None
    return None


def _side(price: float, level: float) -> str:
    return ABOVE if price >= level else BELOW


def evaluate(a: dict, price: float | None, level: float | None) -> dict | None:
    """Advance one alert against a new price. Returns a fire record or None.

    Mutates `a` in place (state, last_price, last_level) — the caller owns
    persistence.
    """
    a["last_eval"] = _now()
    if price is None or level is None or price <= 0 or level <= 0:
        return None  # cannot judge a crossing without both halves

    prev_price = a.get("last_price")
    a["last_price"], a["last_level"] = price, level

    if a["state"] == "triggered" and not a["repeat"]:
        return None

    arm_side = ABOVE if a["direction"] == BELOW else BELOW
    side_now = _side(price, level)

    # Cooldown applies to repeat alerts between fires.
    if a["state"] == "triggered" and a["repeat"]:
        if _now() - (a["triggered_at"] or 0) < a["cooldown"]:
            return None
        a["state"] = "pending"

    if a["state"] == "pending":
        # Only arm once price is on the side the break would start from. This is
        # what stops "alert me when SPY breaks below 743" from firing instantly
        # when SPY is already at 741.
        if side_now == arm_side:
            a["state"] = "armed"
        return None

    if prev_price is None:
        return None  # armed this cycle; no previous price to cross from

    # A dynamic level can move ACROSS a stationary price, leaving an "armed"
    # alert sitting on the wrong side of its own level without price having done
    # anything. That is not a break, so it must not fire — but leaving it armed
    # is a lie the UI repeats, and the stale prev_price would then let a later
    # move fire against a crossing that never happened. Drop back to pending:
    # it re-arms the moment price is on the arming side again, and fires on the
    # next genuine crossing.
    if _side(prev_price, level) != arm_side and side_now != arm_side:
        a["state"] = "pending"
        return None

    # Both prices judged against the CURRENT level, so a level that moved across
    # a stationary price cannot manufacture a crossing.
    if _side(prev_price, level) == arm_side and side_now != arm_side:
        a["state"] = "triggered"
        a["triggered_at"] = _now()
        a["trigger_count"] += 1
        return {
            "id": a["id"], "symbol": a["symbol"], "direction": a["direction"],
            "level": level, "level_ref": a["level_ref"], "price": price,
            "prev_price": prev_price, "note": a["note"],
            "text": describe(a, level), "at": a["triggered_at"],
        }

    # Price came back to the arming side after a level move: nothing to report,
    # the alert simply stays armed.
    return None


class AlertStore:
    """Alerts on disk, guarded by a lock.

    The watcher thread and the HTTP handlers both touch this, so every public
    method takes the lock and writes are atomic (temp file + replace) — a torn
    write here would lose every alert on the next boot.
    """

    def __init__(self, path: Path = STORE_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._alerts: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text())
            return [dict(a) for a in raw] if isinstance(raw, list) else []
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError):
            # A corrupt store must not stop the app from booting; alerts are
            # recreatable, an unbootable panel is not.
            return []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._alerts, indent=2))
            tmp.replace(self.path)
        except OSError:
            pass  # keep serving from memory rather than crash the watcher

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(a) for a in self._alerts]

    def add(self, a: dict) -> dict:
        with self._lock:
            self._alerts.append(a)
            self._save()
            return dict(a)

    def remove(self, alert_id: str) -> bool:
        with self._lock:
            n = len(self._alerts)
            self._alerts = [a for a in self._alerts if a["id"] != alert_id]
            if len(self._alerts) != n:
                self._save()
                return True
            return False

    def clear_triggered(self) -> int:
        with self._lock:
            n = len(self._alerts)
            self._alerts = [a for a in self._alerts
                            if not (a["state"] == "triggered" and not a["repeat"])]
            removed = n - len(self._alerts)
            if removed:
                self._save()
            return removed

    def symbols(self) -> list[str]:
        """Distinct symbols still worth watching."""
        with self._lock:
            return sorted({a["symbol"] for a in self._alerts
                           if a["state"] != "triggered" or a["repeat"]})

    def sweep(self, price_of: Callable[[str], float | None],
              levels_of: Callable[[str], dict | None]) -> list[dict]:
        """Evaluate every live alert once. Returns the fire records.

        Both lookups are injected so the watcher owns quote/structure fetching
        and this stays testable without a broker or a network.
        """
        fired: list[dict] = []
        with self._lock:
            dirty = False
            cache: dict[str, Any] = {}
            for a in self._alerts:
                if a["state"] == "triggered" and not a["repeat"]:
                    continue
                sym = a["symbol"]
                try:
                    price = price_of(sym)
                    if sym not in cache:
                        cache[sym] = levels_of(sym) if a["level_ref"] else None
                    level = resolve_level(a, cache.get(sym))
                except Exception:
                    continue  # one bad symbol must not stop the sweep
                before = (a["state"], a["last_price"])
                rec = evaluate(a, price, level)
                if rec:
                    fired.append(rec)
                if rec or before != (a["state"], a["last_price"]):
                    dirty = True
            if dirty:
                self._save()
        return fired
