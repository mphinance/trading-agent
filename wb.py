"""Webull OpenAPI client wrapper.

Thin layer over webull-openapi-python-sdk: credential loading, a short TTL cache
sized to Webull's rate limits, and normalised portfolio shapes for the UI.

Read-only. This wrapper only ever reads accounts/balances/positions — it holds
no order-placement calls, by design (sidecar is a companion, not a second way
to trade).
"""

from __future__ import annotations

import logging
import threading
import time
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
