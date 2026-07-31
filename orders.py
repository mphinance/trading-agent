"""The order path.

This is the only module in sidecar that can move money. Everything else reads.

Design notes, because this reverses the repo's original "no write path" rule and
the reasoning should survive the person who wrote it:

1. **Preview, then confirm, then place.** `preview()` runs the guards, asks
   Webull to price the order, and stages a ticket. `place()` takes a ticket id,
   not an order — so a stray POST cannot construct and fire an order in one shot,
   and the thing you confirm is byte-for-byte the thing that gets sent (the
   ticket stores a hash of the payload). Set `SIDECAR_ORDER_CONFIRM=0` to allow
   single-shot placement.

2. **Guards are here, not in the UI.** The panel is one client; the API is the
   real surface. Notional cap, quantity cap, optional symbol allowlist and the
   buying-power check all run server-side on every path, including replace.

3. **Chat cannot reach this module.** `chat.py` gets read-only tools plus the
   ability to *stage* a ticket for you to confirm in the UI. It holds WebFetch
   and WebSearch, which means any page it reads is an untrusted instruction
   source — a model that can both read the web and place orders is a
   prompt-injection target with your account as the payload. Flip
   `SIDECAR_CHAT_AUTOTRADE=1` if you want that anyway; it is off by default and
   staging still works without it.

4. **This server has no authentication.** That is survivable for a read-only
   deck on a tailnet. With an order path it means anyone who reaches the port
   can trade. Keep it bound to Tailscale or loopback (deploy/install.sh already
   refuses anything else) and treat `SIDECAR_TRADING=0` as the kill switch.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Configuration. Everything here is an env override so you can tighten or
# loosen without a code change — but the defaults are deliberately finite.

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


TRADING_ENABLED = _env_bool("SIDECAR_TRADING", True)
REQUIRE_CONFIRM = _env_bool("SIDECAR_ORDER_CONFIRM", True)
CHAT_AUTOTRADE = _env_bool("SIDECAR_CHAT_AUTOTRADE", False)

# Sized for a small account. A fat-fingered quantity is the single most likely
# way to lose money here, and it costs nothing to make the cap explicit.
MAX_NOTIONAL = _env_float("SIDECAR_MAX_NOTIONAL", 2500.0)
MAX_QUANTITY = _env_float("SIDECAR_MAX_QUANTITY", 10000.0)
# Fraction of buying power a single order may consume. 1.0 disables the check.
MAX_BP_FRACTION = _env_float("SIDECAR_MAX_BP_FRACTION", 1.0)

# Comma-separated. Empty means "any symbol".
_allow = os.environ.get("SIDECAR_SYMBOL_ALLOWLIST", "").strip()
SYMBOL_ALLOWLIST = {s.strip().upper() for s in _allow.split(",") if s.strip()}

TICKET_TTL_SEC = 120.0

VALID_SIDES = {"BUY", "SELL", "SHORT"}
VALID_TIF = {"DAY", "GTC", "IOC"}
VALID_ORDER_TYPES = {
    "MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_LIMIT", "TRAILING_STOP_LOSS",
    "ENHANCED_LIMIT", "AT_AUCTION", "AT_AUCTION_LIMIT", "ODD_LOT_LIMIT",
    "MARKET_ON_OPEN", "MARKET_ON_CLOSE",
}
VALID_SESSIONS = {"CORE", "ALL", "NIGHT", "ALL_DAY", "N"}
VALID_ALGO = {"TWAP", "VWAP", "POV"}
VALID_ENTRUST = {"QTY", "AMOUNT"}


class OrderError(ValueError):
    """Rejected before anything reached the broker."""


class TradingDisabled(OrderError):
    pass


# ---------------------------------------------------------------------------


def _d(v: Any) -> str | None:
    """Webull wants decimals as strings; None means 'omit the field'.

    Floats are normalised so a whole number doesn't become "2.0" — MCP hands
    every number over as a float, and that string ends up both in the payload
    and in the summary Claude reads back out loud.
    """
    if v is None or v == "":
        return None
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        # Trim float repr noise (0.1+0.2 cases) without imposing a fixed scale.
        return f"{v:.10f}".rstrip("0").rstrip(".")
    return str(v)


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Ticket:
    __slots__ = ("id", "kind", "account_id", "payload", "digest", "preview",
                 "created_at", "used", "origin", "summary")

    def __init__(self, kind: str, account_id: str, payload: Any, preview: Any,
                 origin: str, summary: str) -> None:
        self.id = uuid.uuid4().hex
        self.kind = kind          # equity | option | batch
        self.account_id = account_id
        self.payload = payload
        self.digest = _digest(payload)
        self.preview = preview
        self.created_at = time.time()
        self.used = False
        self.origin = origin      # ui | chat — recorded so staged tickets are visible
        self.summary = summary

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > TICKET_TTL_SEC

    def as_dict(self) -> dict:
        return {
            "ticket_id": self.id,
            "kind": self.kind,
            "account_id": self.account_id,
            "summary": self.summary,
            "origin": self.origin,
            "orders": self.payload,
            "preview": self.preview,
            "expires_in": max(0, int(TICKET_TTL_SEC - (time.time() - self.created_at))),
            "used": self.used,
        }


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class Orders:
    """Full order surface over the shared Webull client."""

    def __init__(self, webull) -> None:
        self._wb = webull
        self._t = webull.trade
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()

    # -- guards -----------------------------------------------------------

    def _reap(self) -> None:
        for tid in [t for t, tk in self._tickets.items() if tk.expired]:
            self._tickets.pop(tid, None)

    def _check_enabled(self) -> None:
        if not TRADING_ENABLED:
            raise TradingDisabled("trading disabled (SIDECAR_TRADING=0)")

    def _notional(self, order: dict) -> float:
        """Best available estimate of what the order commits.

        A market order has no limit price, so fall back to the live quote and
        finally to the last known position price. If we genuinely can't price it,
        return 0 rather than guessing high — the buying-power check still applies
        and Webull rejects what it won't fund.
        """
        qty = _num(order.get("quantity"))
        if order.get("entrust_type") == "AMOUNT":
            return _num(order.get("amount") or order.get("quantity"))
        px = _num(order.get("limit_price")) or _num(order.get("stop_price"))
        if not px:
            px = self._last_price(order.get("symbol", ""))
        mult = 100.0 if str(order.get("instrument_type", "")).upper() == "OPTION" else 1.0
        return qty * px * mult

    def _last_price(self, symbol: str) -> float:
        if not symbol:
            return 0.0
        try:
            from server import market  # late import: server owns the singletons
            q = market().snapshot([symbol]).get(symbol) or {}
            if q.get("last"):
                return float(q["last"])
        except Exception:
            pass
        try:
            for p in self._wb.portfolio().get("positions", []):
                if p["symbol"] == symbol and p["last_price"]:
                    return float(p["last_price"])
        except Exception:
            pass
        return 0.0

    def _validate(self, order: dict) -> None:
        sym = str(order.get("symbol", "")).upper()
        if not sym and not order.get("legs"):
            raise OrderError("symbol is required")
        if SYMBOL_ALLOWLIST and sym and sym not in SYMBOL_ALLOWLIST:
            raise OrderError(f"{sym} is not in SIDECAR_SYMBOL_ALLOWLIST")

        side = str(order.get("side", "")).upper()
        if side and side not in VALID_SIDES:
            raise OrderError(f"side must be one of {sorted(VALID_SIDES)}")

        ot = str(order.get("order_type", "")).upper()
        if ot and ot not in VALID_ORDER_TYPES:
            raise OrderError(f"order_type must be one of {sorted(VALID_ORDER_TYPES)}")

        tif = str(order.get("time_in_force", "")).upper()
        if tif and tif not in VALID_TIF:
            raise OrderError(f"time_in_force must be one of {sorted(VALID_TIF)}")

        sess = str(order.get("support_trading_session", "")).upper()
        if sess and sess not in VALID_SESSIONS:
            raise OrderError(f"support_trading_session must be one of {sorted(VALID_SESSIONS)}")

        et = str(order.get("entrust_type", "")).upper()
        if et and et not in VALID_ENTRUST:
            raise OrderError(f"entrust_type must be one of {sorted(VALID_ENTRUST)}")

        qty = _num(order.get("quantity"))
        if qty <= 0:
            raise OrderError("quantity must be > 0")
        if qty > MAX_QUANTITY:
            raise OrderError(f"quantity {qty:g} exceeds SIDECAR_MAX_QUANTITY ({MAX_QUANTITY:g})")

        if ot in ("LIMIT", "STOP_LOSS_LIMIT", "ENHANCED_LIMIT") and not order.get("limit_price"):
            raise OrderError(f"{ot} requires limit_price")
        if ot in ("STOP_LOSS", "STOP_LOSS_LIMIT") and not order.get("stop_price"):
            raise OrderError(f"{ot} requires stop_price")

        algo = str(order.get("algo_type", "")).upper()
        if algo:
            if algo not in VALID_ALGO:
                raise OrderError(f"algo_type must be one of {sorted(VALID_ALGO)}")
            if algo in ("TWAP", "VWAP") and order.get("max_target_percent") is None:
                raise OrderError(f"{algo} requires max_target_percent")
            if algo == "POV" and order.get("target_vol_percent") is None:
                raise OrderError("POV requires target_vol_percent")

        notional = self._notional(order)
        if notional > MAX_NOTIONAL:
            raise OrderError(
                f"order notional ~${notional:,.2f} exceeds SIDECAR_MAX_NOTIONAL "
                f"(${MAX_NOTIONAL:,.2f}). Raise the cap deliberately if you mean it."
            )
        if MAX_BP_FRACTION < 1.0 and side in ("BUY", "SHORT") and notional:
            try:
                bp = self._wb.portfolio()["totals"]["buying_power"]
            except Exception:
                bp = 0.0
            if bp and notional > bp * MAX_BP_FRACTION:
                raise OrderError(
                    f"order notional ~${notional:,.2f} exceeds {MAX_BP_FRACTION:.0%} "
                    f"of ${bp:,.2f} buying power"
                )

    # -- payload builders -------------------------------------------------

    def build_equity(self, spec: dict) -> list[dict]:
        """Normalise a UI order spec into Webull v3 payload(s).

        A `bracket` turns one spec into a MASTER + STOP_PROFIT + STOP_LOSS combo,
        which is how Webull expresses an OTOCO.
        """
        coid = spec.get("client_order_id") or uuid.uuid4().hex
        base = {
            "client_order_id": coid,
            "combo_type": "NORMAL",
            "symbol": str(spec["symbol"]).upper(),
            "instrument_type": str(spec.get("instrument_type", "EQUITY")).upper(),
            "market": str(spec.get("market", "US")).upper(),
            "order_type": str(spec.get("order_type", "LIMIT")).upper(),
            "quantity": _d(spec.get("quantity")),
            "side": str(spec.get("side", "BUY")).upper(),
            "time_in_force": str(spec.get("time_in_force", "DAY")).upper(),
            "entrust_type": str(spec.get("entrust_type", "QTY")).upper(),
            "support_trading_session": str(spec.get("trading_session", "CORE")).upper(),
        }
        for k_src, k_dst in (
            ("limit_price", "limit_price"), ("stop_price", "stop_price"),
            ("trailing_type", "trailing_type"), ("trailing_stop_step", "trailing_stop_step"),
            ("algo_type", "algo_type"), ("algo_start_time", "algo_start_time"),
            ("algo_end_time", "algo_end_time"), ("max_target_percent", "max_target_percent"),
            ("target_vol_percent", "target_vol_percent"), ("amount", "amount"),
        ):
            v = _d(spec.get(k_src))
            if v is not None:
                base[k_dst] = v

        bracket = spec.get("bracket") or {}
        tp, sl = bracket.get("take_profit"), bracket.get("stop_loss")
        if not tp and not sl:
            return [base]

        # Bracket: the master opens, the children close. Children take the
        # opposite side and the same quantity as the master.
        close_side = "SELL" if base["side"] == "BUY" else "BUY"
        base["combo_type"] = "MASTER"
        orders = [base]
        common = {
            "symbol": base["symbol"], "instrument_type": base["instrument_type"],
            "market": base["market"], "quantity": base["quantity"],
            "side": close_side, "entrust_type": "QTY",
            "time_in_force": base["time_in_force"],
            "support_trading_session": base["support_trading_session"],
        }
        if tp:
            orders.append({**common, "client_order_id": uuid.uuid4().hex,
                           "combo_type": "STOP_PROFIT", "order_type": "LIMIT",
                           "limit_price": _d(tp)})
        if sl:
            orders.append({**common, "client_order_id": uuid.uuid4().hex,
                           "combo_type": "STOP_LOSS", "order_type": "STOP_LOSS",
                           "stop_price": _d(sl)})
        return orders

    def build_option(self, spec: dict) -> list[dict]:
        """Single-leg or multi-leg option order (verticals, spreads, condors...)."""
        coid = spec.get("client_order_id") or uuid.uuid4().hex
        legs = []
        for leg in spec.get("legs") or []:
            legs.append({
                "side": str(leg.get("side", spec.get("side", "BUY"))).upper(),
                "quantity": _d(leg.get("quantity", spec.get("quantity"))),
                "symbol": str(leg["symbol"]).upper(),
                "strike_price": _d(leg.get("strike_price")),
                "option_expire_date": leg.get("option_expire_date"),
                "instrument_type": "OPTION",
                "option_type": str(leg.get("option_type", "CALL")).upper(),
                "market": str(leg.get("market", "US")).upper(),
            })
        if not legs:
            raise OrderError("option order requires at least one leg")
        order = {
            "client_order_id": coid,
            "combo_type": "NORMAL",
            "order_type": str(spec.get("order_type", "LIMIT")).upper(),
            "quantity": _d(spec.get("quantity", legs[0]["quantity"])),
            "option_strategy": str(spec.get("option_strategy", "SINGLE")).upper(),
            "side": str(spec.get("side", "BUY")).upper(),
            "time_in_force": str(spec.get("time_in_force", "DAY")).upper(),
            "entrust_type": str(spec.get("entrust_type", "QTY")).upper(),
            "legs": legs,
        }
        lp = _d(spec.get("limit_price"))
        if lp is not None:
            order["limit_price"] = lp
        return [order]

    # -- preview / stage --------------------------------------------------

    def preview(self, spec: dict, kind: str = "equity", origin: str = "ui") -> dict:
        """Validate, price with Webull, and stage a ticket for confirmation."""
        self._check_enabled()
        account_id = spec.get("account_id") or (self._wb.account_ids() or [None])[0]
        if not account_id:
            raise OrderError("no account available")

        payload = self.build_option(spec) if kind == "option" else self.build_equity(spec)
        for o in payload:
            merged = {**o, "instrument_type": o.get("instrument_type", "OPTION" if kind == "option" else "EQUITY")}
            self._validate(merged)

        if kind == "option":
            res = self._t.order_v2.preview_option(account_id, payload)
        else:
            res = self._t.order_v3.preview_order(account_id, payload)
        preview = _body(res)

        ticket = Ticket(kind, account_id, payload, preview, origin, _summarise(payload, kind))
        with self._lock:
            self._reap()
            self._tickets[ticket.id] = ticket
        return ticket.as_dict()

    def pending_tickets(self) -> list[dict]:
        with self._lock:
            self._reap()
            return [t.as_dict() for t in self._tickets.values() if not t.used]

    def discard(self, ticket_id: str) -> bool:
        with self._lock:
            return self._tickets.pop(ticket_id, None) is not None

    # -- place ------------------------------------------------------------

    def place_ticket(self, ticket_id: str) -> dict:
        """Send a previously previewed ticket. The only path when confirm is on."""
        self._check_enabled()
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise OrderError("unknown or expired ticket")
            if ticket.used:
                raise OrderError("ticket already placed")
            if ticket.expired:
                self._tickets.pop(ticket_id, None)
                raise OrderError("ticket expired — preview again")
            if _digest(ticket.payload) != ticket.digest:
                raise OrderError("ticket payload changed since preview")
            ticket.used = True

        try:
            return self._send(ticket.kind, ticket.account_id, ticket.payload)
        except Exception:
            with self._lock:
                ticket.used = False  # let the user retry the same confirmed ticket
            raise

    def place_direct(self, spec: dict, kind: str = "equity") -> dict:
        """Single-shot placement. Only reachable with SIDECAR_ORDER_CONFIRM=0."""
        self._check_enabled()
        if REQUIRE_CONFIRM:
            raise OrderError("confirmation required: preview first, then place the ticket")
        account_id = spec.get("account_id") or (self._wb.account_ids() or [None])[0]
        if not account_id:
            raise OrderError("no account available")
        payload = self.build_option(spec) if kind == "option" else self.build_equity(spec)
        for o in payload:
            self._validate(o)
        return self._send(kind, account_id, payload)

    def _send(self, kind: str, account_id: str, payload: list[dict]) -> dict:
        if kind == "option":
            res = self._t.order_v2.place_option(account_id, payload)
        else:
            res = self._t.order_v3.place_order(account_id, payload)
        body = _body(res)
        # A fill invalidates positions, balance and the open-order list on the
        # spot; serving the cached snapshot after this would misreport holdings.
        self._wb.invalidate("portfolio", "open_orders")
        return {"ok": True, "account_id": account_id, "orders": payload, "response": body}

    def batch_place(self, specs: list[dict], account_id: str | None = None) -> dict:
        self._check_enabled()
        if REQUIRE_CONFIRM:
            raise OrderError("confirmation required: preview each order, then place its ticket")
        aid = account_id or (self._wb.account_ids() or [None])[0]
        if not aid:
            raise OrderError("no account available")
        batch: list[dict] = []
        for spec in specs:
            built = self.build_equity(spec)
            for o in built:
                self._validate(o)
            batch.extend(built)
        res = self._t.order_v3.batch_place_order(aid, batch)
        self._wb.invalidate("portfolio", "open_orders")
        return {"ok": True, "account_id": aid, "orders": batch, "response": _body(res)}

    # -- modify / cancel --------------------------------------------------

    def replace(self, account_id: str, client_order_id: str, changes: dict,
                kind: str = "equity") -> dict:
        """Amend a working order. Guards apply — a replace can raise exposure."""
        self._check_enabled()
        mod: dict = {"client_order_id": client_order_id}
        for k in ("quantity", "limit_price", "stop_price", "algo_start_time",
                  "algo_end_time", "max_target_percent", "target_vol_percent"):
            v = _d(changes.get(k))
            if v is not None:
                mod[k] = v
        if len(mod) == 1:
            raise OrderError("nothing to change")

        if "quantity" in mod or "limit_price" in mod:
            probe = {
                "symbol": changes.get("symbol", ""),
                "quantity": mod.get("quantity", changes.get("quantity", 0)),
                "limit_price": mod.get("limit_price"),
                "side": str(changes.get("side", "BUY")).upper(),
                "order_type": str(changes.get("order_type", "LIMIT")).upper(),
                "instrument_type": "OPTION" if kind == "option" else "EQUITY",
                "entrust_type": "QTY",
            }
            self._validate(probe)

        if kind == "option":
            res = self._t.order_v2.replace_option(account_id, [mod])
        else:
            res = self._t.order_v3.replace_order(account_id, [mod])
        self._wb.invalidate("open_orders")
        return {"ok": True, "response": _body(res)}

    def cancel(self, account_id: str, client_order_id: str, kind: str = "equity") -> dict:
        """Cancelling reduces exposure, so it is never gated by the caps."""
        self._check_enabled()
        if kind == "option":
            res = self._t.order_v2.cancel_option(account_id, client_order_id)
        else:
            res = self._t.order_v3.cancel_order(account_id, client_order_id)
        self._wb.invalidate("open_orders")
        return {"ok": True, "response": _body(res)}

    def cancel_all(self) -> dict:
        """Flatten the working-order book. Does not touch positions."""
        self._check_enabled()
        results = []
        for o in self._wb.open_orders():
            coid = o.get("client_order_id")
            if not coid:
                continue
            kind = "option" if "OPTION" in str(o.get("instrument_type", "")).upper() else "equity"
            try:
                self.cancel(o["account_id"], coid, kind)
                results.append({"client_order_id": coid, "ok": True})
            except Exception as e:
                results.append({"client_order_id": coid, "ok": False, "error": str(e)})
        return {"cancelled": results}

    # -- introspection ----------------------------------------------------

    @staticmethod
    def config() -> dict:
        return {
            "trading_enabled": TRADING_ENABLED,
            "require_confirm": REQUIRE_CONFIRM,
            "chat_autotrade": CHAT_AUTOTRADE,
            "max_notional": MAX_NOTIONAL,
            "max_quantity": MAX_QUANTITY,
            "max_bp_fraction": MAX_BP_FRACTION,
            "symbol_allowlist": sorted(SYMBOL_ALLOWLIST) or None,
            "ticket_ttl_sec": TICKET_TTL_SEC,
        }


def _body(res) -> Any:
    """Unwrap an SDK response, surfacing broker rejections as errors.

    The SDK returns a requests.Response; a non-200 carries Webull's own reason,
    which is far more useful than a generic failure.
    """
    code = getattr(res, "status_code", 200)
    try:
        body = res.json()
    except Exception:
        body = {"raw": getattr(res, "text", "")[:500]}
    if code != 200:
        msg = ""
        if isinstance(body, dict):
            msg = body.get("msg") or body.get("message") or body.get("error") or ""
        raise OrderError(f"Webull rejected the order (HTTP {code}): {msg or body}")
    return body


def _summarise(payload: list[dict], kind: str) -> str:
    """One line a human can check before confirming."""
    if not payload:
        return "empty order"
    head = payload[0]
    if kind == "option":
        legs = head.get("legs") or []
        leg_txt = ", ".join(
            f"{l.get('side')} {l.get('quantity')}x {l.get('symbol')} "
            f"{l.get('option_expire_date')} {l.get('strike_price')}{str(l.get('option_type', ''))[:1]}"
            for l in legs
        )
        px = f" @ {head.get('limit_price')}" if head.get("limit_price") else " @ MKT"
        return f"{head.get('option_strategy', 'SINGLE')}: {leg_txt}{px} {head.get('time_in_force', '')}"

    px = ""
    if head.get("limit_price"):
        px = f" @ {head['limit_price']}"
    elif head.get("stop_price"):
        px = f" stop {head['stop_price']}"
    elif head.get("order_type") == "MARKET":
        px = " @ MKT"
    algo = f" [{head['algo_type']}]" if head.get("algo_type") else ""
    line = (f"{head.get('side')} {head.get('quantity')} {head.get('symbol')}"
            f"{px} {head.get('time_in_force', '')} {head.get('support_trading_session', '')}".rstrip())
    extra = [o for o in payload[1:]]
    if extra:
        bits = []
        for o in extra:
            if o.get("combo_type") == "STOP_PROFIT":
                bits.append(f"TP {o.get('limit_price')}")
            elif o.get("combo_type") == "STOP_LOSS":
                bits.append(f"SL {o.get('stop_price')}")
        if bits:
            line += " (" + " / ".join(bits) + ")"
    return line + algo
