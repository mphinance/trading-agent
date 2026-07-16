"""Webull OpenAPI client wrapper.

Thin layer over webull-openapi-python-sdk: credential loading, a short TTL cache
sized to Webull's rate limits, and normalised portfolio shapes for the UI.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logging.getLogger("webull").setLevel(logging.CRITICAL)

from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

ENV_PATH = Path(__file__).resolve().parent.parent / ".env.webull"
PROD_HOST = "api.webull.com"
REGION = "us"

# Balance and positions are limited to 2 requests / 2 seconds EACH. A single
# portfolio poll spends that entire budget (one call per endpoint per account,
# and there are two accounts), so refreshes must stay >2s apart and concurrent
# callers must never trigger a second fetch. Hence the lock and the stale
# fallback below — without them the page load races itself into a 429.
CACHE_TTL_SEC = 8.0
PACE_SEC = 0.35  # spacing between per-account calls
RETRY_ON_429 = 2
BACKOFF_SEC = 2.2


class WebullError(RuntimeError):
    pass


def _load_env(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        raise WebullError(f"credentials file not found: {path}")
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Webull:
    def __init__(self) -> None:
        env = _load_env()
        key, secret = env.get("WEBULL_KEY"), env.get("WEBULL_SECRET")
        if not key or not secret:
            raise WebullError("WEBULL_KEY / WEBULL_SECRET missing from .env.webull")
        client = ApiClient(key, secret, REGION)
        client.add_endpoint(REGION, PROD_HOST)
        self._trade = TradeClient(client)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._last_good: dict | None = None
        self._last_error: str | None = None

    def _cached(self, key: str, fn, ttl: float = CACHE_TTL_SEC):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    def invalidate(self) -> None:
        """Drop cached portfolio state (call after an order changes it)."""
        self._cache.pop("portfolio", None)

    @staticmethod
    def _is_429(e: Exception) -> bool:
        return "429" in str(e) or "TOO_MANY_REQUESTS" in str(e)

    def _retrying(self, fn):
        """Retry once or twice on 429 — the limit is per 2s, so a short wait clears it."""
        for attempt in range(RETRY_ON_429 + 1):
            try:
                return fn()
            except Exception as e:
                if self._is_429(e) and attempt < RETRY_ON_429:
                    time.sleep(BACKOFF_SEC)
                    continue
                raise

    def accounts(self) -> list[dict]:
        return self._cached("accounts", lambda: self._trade.account_v2.get_account_list().json(), ttl=60.0)

    def equity_account_id(self) -> str:
        for a in self.accounts():
            if a.get("account_class") == "INDIVIDUAL_CASH":
                return a["account_id"]
        raise WebullError("no INDIVIDUAL_CASH account found")

    def equity_account_number(self) -> str:
        """Human-facing account number — its last 4 is the place-order confirmation."""
        for a in self.accounts():
            if a.get("account_class") == "INDIVIDUAL_CASH":
                return a.get("account_number", "")
        return ""

    def portfolio(self) -> dict:
        """Every account with balances and positions, plus rolled-up totals.

        Concurrent callers share a single fetch. If the refresh fails (usually a
        429), the last good snapshot is served with `stale: True` rather than
        propagating a 500 to the UI.
        """
        with self._lock:
            try:
                p = self._cached("portfolio", self._portfolio_uncached)
                self._last_good, self._last_error = p, None
                return {**p, "stale": False, "error": None}
            except Exception as e:
                self._last_error = str(e)
                if self._last_good is None:
                    raise
                return {**self._last_good, "stale": True, "error": self._last_error}

    def _portfolio_uncached(self) -> dict:
        accounts = []
        for i, a in enumerate(self.accounts()):
            aid = a["account_id"]
            if i:
                time.sleep(PACE_SEC)  # stay inside the 2 req / 2s window
            balance = self._retrying(lambda: self._trade.account_v2.get_account_balance(aid).json())
            positions = self._retrying(lambda: self._trade.account_v2.get_account_position(aid).json()) or []
            assets = (balance.get("account_currency_assets") or [{}])[0]
            accounts.append(
                {
                    "account_id": aid,
                    "label": a.get("account_label", "?"),
                    "account_class": a.get("account_class", "?"),
                    "nlv": _f(balance.get("total_net_liquidation_value")),
                    "cash": _f(balance.get("total_cash_balance")),
                    "market_value": _f(balance.get("total_market_value")),
                    "day_pl": _f(balance.get("total_day_profit_loss")),
                    "unrealized_pl": _f(balance.get("total_unrealized_profit_loss")),
                    "buying_power": _f(assets.get("buying_power")),
                    "option_buying_power": _f(assets.get("option_buying_power")),
                    "overnight_buying_power": _f(assets.get("night_trading_buying_power")),
                    "positions": [_position(p, aid) for p in positions],
                }
            )

        all_pos = [p for a in accounts for p in a["positions"]]
        total_nlv = sum(a["nlv"] for a in accounts)
        total_cost = sum(p["cost"] for p in all_pos)
        total_mv = sum(p["market_value"] for p in all_pos)
        total_upl = sum(p["unrealized_pl"] for p in all_pos)

        # Buying power is shared across accounts, so max() not sum() — summing
        # would double-count the same dollars and overstate available capital.
        buying_power = max((a["buying_power"] for a in accounts), default=0.0)

        return {
            "accounts": accounts,
            "positions": sorted(all_pos, key=lambda p: p["market_value"], reverse=True),
            "totals": {
                "nlv": total_nlv,
                "cost": total_cost,
                "market_value": total_mv,
                "unrealized_pl": total_upl,
                "unrealized_pl_pct": (total_upl / total_cost) if total_cost else 0.0,
                "day_pl": sum(a["day_pl"] for a in accounts),
                "buying_power": buying_power,
                "position_count": len(all_pos),
                "winners": sum(1 for p in all_pos if p["unrealized_pl"] > 0),
                "losers": sum(1 for p in all_pos if p["unrealized_pl"] < 0),
            },
            "fetched_at": time.time(),
        }

    def _order_payload(self, symbol: str, side: str, qty: str, limit_price: str,
                       client_order_id: str | None = None) -> list[dict]:
        # combo_type is mandatory on equity orders — Webull's own bundled sample
        # omits it and the request fails with 417 invalid combo_type.
        #
        # client_order_id is minted at preview and carried through to place, so
        # an ambiguous timeout + retry is rejected by Webull as a duplicate
        # rather than filling twice. Never regenerate it on the place path.
        return [
            {
                "combo_type": "NORMAL",
                "client_order_id": client_order_id or uuid.uuid4().hex,
                "symbol": symbol.upper().strip(),
                "instrument_type": "EQUITY",
                "market": "US",
                "order_type": "LIMIT",
                "limit_price": str(limit_price),
                "quantity": str(qty),
                "support_trading_session": "CORE",
                "side": side.upper().strip(),
                "time_in_force": "DAY",
                "entrust_type": "QTY",
            }
        ]

    def preview(self, symbol: str, side: str, qty: str, limit_price: str,
                client_order_id: str | None = None) -> dict:
        aid = self.equity_account_id()
        orders = self._order_payload(symbol, side, qty, limit_price, client_order_id)
        res = self._trade.order_v2.preview_order(aid, orders)
        return {"http": res.status_code, "payload": orders[0], "result": res.json()}

    def place(self, symbol: str, side: str, qty: str, limit_price: str,
              client_order_id: str | None = None) -> dict:
        """Place a REAL order. Pass the client_order_id from preview — see _order_payload."""
        aid = self.equity_account_id()
        orders = self._order_payload(symbol, side, qty, limit_price, client_order_id)
        res = self._trade.order_v2.place_order(aid, orders)
        self.invalidate()
        return {"http": res.status_code, "payload": orders[0], "result": res.json()}

    def held_qty(self, symbol: str) -> float:
        """Shares currently held. Used to block an oversell → accidental short."""
        for p in self.portfolio()["positions"]:
            if p["symbol"] == symbol.upper().strip():
                return p["quantity"]
        return 0.0

    def open_sell_qty(self, symbol: str) -> float:
        """Quantity already working on the sell side, so two half-sells can't both pass."""
        total = 0.0
        for o in self.open_orders():
            if (o.get("symbol", "").upper() == symbol.upper().strip()
                    and str(o.get("side", "")).upper() == "SELL"):
                try:
                    total += float(o.get("quantity", 0)) - float(o.get("filled_quantity", 0) or 0)
                except (TypeError, ValueError):
                    pass
        return total

    def open_orders(self) -> list[dict]:
        aid = self.equity_account_id()
        try:
            res = self._trade.order_v2.get_order_open(aid)
            return res.json() if res.status_code == 200 else []
        except Exception:
            return []

    def cancel(self, client_order_id: str) -> dict:
        aid = self.equity_account_id()
        res = self._trade.order_v2.cancel_order(aid, client_order_id)
        self.invalidate()
        return {"http": res.status_code, "result": res.json()}


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _position(p: dict, account_id: str) -> dict:
    return {
        "account_id": account_id,
        "symbol": p.get("symbol", "?"),
        "instrument_type": p.get("instrument_type", "?"),
        "quantity": _f(p.get("quantity")),
        "cost": _f(p.get("cost")),
        "cost_price": _f(p.get("cost_price")),
        "last_price": _f(p.get("last_price")),
        "market_value": _f(p.get("market_value")),
        "unrealized_pl": _f(p.get("unrealized_profit_loss")),
        "unrealized_pl_pct": _f(p.get("unrealized_profit_loss_rate")),
        "day_pl": _f(p.get("day_profit_loss")),
        "position_id": p.get("position_id", ""),
    }
