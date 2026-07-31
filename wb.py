"""Webull OpenAPI client wrapper.

Thin layer over webull-openapi-python-sdk: credential loading, a short TTL cache
sized to Webull's rate limits, and normalised portfolio shapes for the UI.

This module owns the single authenticated `ApiClient` that the rest of the app
shares — `md.py` (market data), `orders.py` (order path) and `stream.py`
(MQTT/gRPC push) all borrow it rather than building their own. One credential
load, one token file, one 2FA state.

Rate-limit note, US region (from Webull's own api_reference):
    order query          2 req / 2s     <- balance + positions live here
    market data        600 req / min
    order place/cancel 600 req / min
The account endpoints are the scarce ones; market data is ~100x cheaper. Keep
those budgets separate — see `md.py`.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

logging.getLogger("webull").setLevel(logging.CRITICAL)

from webull.core.client import ApiClient
from webull.data.data_client import DataClient
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

# Order queries share the 2 req/2s bucket with balance/positions, so they get
# their own short cache rather than being fetched on every UI tick.
ORDER_CACHE_TTL_SEC = 4.0


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


def credentials() -> tuple[str, str, str]:
    """(app_key, app_secret, region_id) from ../.env.webull.

    Accepts WEBULL_APP_KEY/WEBULL_APP_SECRET as aliases — that's what webull-inc's
    own repos and docs use, so a key pasted from their README works unchanged.
    """
    env = _load_env()
    key = env.get("WEBULL_KEY") or env.get("WEBULL_APP_KEY")
    secret = env.get("WEBULL_SECRET") or env.get("WEBULL_APP_SECRET")
    region = env.get("WEBULL_REGION_ID") or REGION
    if not key or not secret:
        raise WebullError("WEBULL_KEY / WEBULL_SECRET missing from .env.webull")
    return key, secret, region


class Webull:
    def __init__(self) -> None:
        key, secret, region = credentials()
        self.region = region
        client = ApiClient(key, secret, region)
        client.add_endpoint(region, PROD_HOST)
        self._api = client
        self._trade = TradeClient(client)
        self._data = DataClient(client)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._last_good: dict | None = None
        self._last_error: str | None = None

    # -- shared handles ---------------------------------------------------
    # md.py / orders.py / stream.py reuse these rather than re-authenticating.

    @property
    def api(self) -> ApiClient:
        return self._api

    @property
    def trade(self) -> TradeClient:
        return self._trade

    @property
    def data(self) -> DataClient:
        return self._data

    def _cached(self, key: str, fn, ttl: float = CACHE_TTL_SEC):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    def invalidate(self, *keys: str) -> None:
        """Drop cached reads so the next poll is live.

        Called after an order goes in: the position/balance snapshot on hand is
        known-stale the moment a fill lands, and waiting out the TTL makes the
        deck lie about what you're holding.
        """
        for k in keys or tuple(self._cache):
            self._cache.pop(k, None)

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

    def account_ids(self) -> list[str]:
        return [a["account_id"] for a in self.accounts()]

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
        option_bp = max((a["option_buying_power"] for a in accounts), default=0.0)

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
                "option_buying_power": option_bp,
                "position_count": len(all_pos),
                "winners": sum(1 for p in all_pos if p["unrealized_pl"] > 0),
                "losers": sum(1 for p in all_pos if p["unrealized_pl"] < 0),
            },
            "fetched_at": time.time(),
        }

    # -- order-side reads -------------------------------------------------
    # Reading orders is not placing them; these stay here with the other
    # account reads so they share the 2 req/2s bucket's cache discipline.
    # The write path lives in orders.py, alone and on purpose.

    def open_orders(self) -> list[dict]:
        """Working orders across every account — including ones placed in Webull Desktop."""
        def fetch() -> list[dict]:
            out: list[dict] = []
            for i, aid in enumerate(self.account_ids()):
                if i:
                    time.sleep(PACE_SEC)
                res = self._retrying(lambda: self._trade.order_v3.get_order_open(account_id=aid))
                out.extend(_orders(res.json(), aid))
            return out
        return self._cached("open_orders", fetch, ttl=ORDER_CACHE_TTL_SEC)

    def order_history(self, page_size: int = 50) -> list[dict]:
        def fetch() -> list[dict]:
            out: list[dict] = []
            for i, aid in enumerate(self.account_ids()):
                if i:
                    time.sleep(PACE_SEC)
                res = self._retrying(
                    lambda: self._trade.order_v3.get_order_history(aid, page_size=str(page_size))
                )
                out.extend(_orders(res.json(), aid))
            return sorted(out, key=lambda o: o.get("create_time") or "", reverse=True)
        return self._cached(f"order_history:{page_size}", fetch, ttl=15.0)

    def order_detail(self, account_id: str, client_order_id: str) -> dict:
        return self._trade.order_v3.get_order_detail(account_id, client_order_id).json()

    def activities(self, account_id: str | None = None) -> list[dict]:
        """Transaction history — fills, dividends, transfers, fees."""
        aid = account_id or (self.account_ids() or [None])[0]
        if not aid:
            return []
        def fetch():
            res = self._retrying(lambda: self._trade.activity.get_activities(aid))
            data = res.json()
            return data.get("items") or data.get("data") or (data if isinstance(data, list) else [])
        return self._cached(f"activities:{aid}", fetch, ttl=60.0)

    def position_details(self, account_id: str, instrument_id: str) -> Any:
        """Per-lot breakdown of one position. JP-only on Webull's side today."""
        return self._trade.account_v2.get_account_position_details(account_id, instrument_id).json()

    def trade_calendar(self, market: str = "US") -> Any:
        return self._cached(
            f"calendar:{market}",
            lambda: self._trade.trade_calendar.get_trade_calendar(market).json(),
            ttl=3600.0,
        )

    def tradeable(self, symbols: str, category: str = "US_STOCK") -> Any:
        """Whether the account may trade these symbols, and any restriction reason."""
        return self._cached(
            f"tradeable:{category}:{symbols}",
            lambda: self._trade.trade_instrument.get_tradeable_instruments(symbols, category).json(),
            ttl=300.0,
        )


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _orders(payload: Any, account_id: str) -> list[dict]:
    """Normalise an order-list response.

    The v3 endpoints have returned bare lists and {"items": [...]} shaped bodies
    depending on region and page, so accept both rather than index blindly.
    """
    if isinstance(payload, dict):
        rows = payload.get("items") or payload.get("orders") or payload.get("data") or []
    else:
        rows = payload or []
    out = []
    for o in rows:
        if not isinstance(o, dict):
            continue
        legs = o.get("legs") or o.get("items") or []
        head = legs[0] if legs and isinstance(legs[0], dict) else {}
        out.append(
            {
                "account_id": account_id,
                "client_order_id": o.get("client_order_id", ""),
                "order_id": o.get("order_id", ""),
                "symbol": o.get("symbol") or head.get("symbol", "?"),
                "side": o.get("side") or head.get("side", "?"),
                "status": o.get("order_status") or o.get("status", "?"),
                "order_type": o.get("order_type") or head.get("order_type", "?"),
                "quantity": _f(o.get("quantity") or head.get("quantity")),
                "filled_quantity": _f(o.get("filled_quantity") or head.get("filled_quantity")),
                "limit_price": _f(o.get("limit_price") or head.get("limit_price")),
                "stop_price": _f(o.get("stop_price") or head.get("stop_price")),
                "avg_fill_price": _f(o.get("avg_fill_price") or head.get("avg_fill_price")),
                "time_in_force": o.get("time_in_force") or head.get("time_in_force", ""),
                "instrument_type": o.get("instrument_type") or head.get("instrument_type", ""),
                "combo_type": o.get("combo_type", ""),
                "create_time": o.get("create_time") or o.get("created_at") or "",
                "raw": o,
            }
        )
    return out


def _position(p: dict, account_id: str) -> dict:
    return {
        "account_id": account_id,
        "symbol": p.get("symbol", "?"),
        "instrument_id": p.get("instrument_id", ""),
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
