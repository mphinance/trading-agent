"""Last price for a symbol, from whichever source is actually entitled.

There are three sources here and they are not interchangeable, so the order is
deliberate:

1. **Webull data API snapshot** — real quotes, any symbol, batched up to 100 per
   call. This is the right source, and it is the one that may not be available:
   market data is a separate entitlement from trading, and sidecar's credentials
   were only ever exercised against the trade API. If it is not entitled the
   call fails and this degrades instead of taking the watcher down with it.
2. **The portfolio poll** — `last_price` already arrives on every position, so
   held names cost nothing extra and are never rate-limited beyond the poll that
   was happening anyway. Held symbols only, obviously.
3. **TDPro `spotPrice`** — the backstop, and a poor one for alerting: it is
   cached 5 minutes upstream, so a break can be minutes stale. Fine for
   confirming structure, wrong for a trigger. It is here so an alert on an
   unheld symbol still does *something* when 1 is unavailable, and `source` is
   reported so nobody mistakes it for a live quote.

Whichever answers, the price is tagged with its source and age. An alert firing
off a five-minute-old print is a different claim from one firing off a live
quote, and the notification says which.
"""

from __future__ import annotations

import time
from typing import Any

# Batch ceiling from the SDK docstring. The watcher will never approach it, but
# a chunked request is one line and an opaque 400 at symbol 101 is not.
MAX_BATCH = 100

# TDPro caches gamma ~5 min, so a spot read older than this is not worth having.
TD_SPOT_MAX_AGE = 360.0


class Quotes:
    """Last-price lookup with a source chain. Never raises."""

    def __init__(self, wb_data=None, portfolio_fn=None, td=None, on_log=None) -> None:
        self._wb_data = wb_data          # webull DataClient, or None
        self._portfolio_fn = portfolio_fn  # () -> portfolio dict, or None
        self._td = td                    # TDPro, or None
        self._log = on_log or (lambda *a: None)
        self._cache: dict[str, tuple[float, float, str]] = {}  # sym -> (ts, price, source)
        # Latched once the data API refuses, so a watcher tick doesn't retry an
        # unentitled endpoint every few seconds forever.
        self._snapshot_dead = False
        self._snapshot_error: str | None = None

    def set_data_client(self, client) -> None:
        """Attach (or replace) the Webull market-data client, clearing any latch."""
        self._wb_data = client
        self._snapshot_dead = False
        self._snapshot_error = None

    @property
    def snapshot_available(self) -> bool:
        return self._wb_data is not None and not self._snapshot_dead

    def status(self) -> dict:
        """`sources` and `max_age_sec` aggregate the per-symbol cache
        (never symbol-level detail) for metrics.py's record_quote_snapshot --
        see watcher.py's _tick(), the one caller. source_of()/age_of() above
        already expose the per-symbol detail this deliberately does NOT
        duplicate."""
        now = time.time()
        sources: dict[str, int] = {}
        max_age = 0.0
        for _sym, (ts, _price, source) in self._cache.items():
            sources[source] = sources.get(source, 0) + 1
            max_age = max(max_age, now - ts)
        return {
            "snapshot": "ok" if self.snapshot_available else (self._snapshot_error or "unavailable"),
            "cached": len(self._cache),
            "sources": sources,
            "max_age_sec": max_age if self._cache else None,
        }

    def refresh(self, symbols: list[str]) -> dict[str, float]:
        """Fetch a batch and populate the cache. Returns {symbol: price}."""
        symbols = [s.upper() for s in symbols if s]
        if not symbols:
            return {}
        out: dict[str, float] = {}

        if self.snapshot_available:
            for i in range(0, len(symbols), MAX_BATCH):
                out.update(self._snapshot(symbols[i:i + MAX_BATCH]))

        missing = [s for s in symbols if s not in out]
        if missing:
            out.update(self._from_portfolio(missing))
        missing = [s for s in symbols if s not in out]
        for s in missing:
            p = self._from_td(s)
            if p is not None:
                out[s] = p
        return out

    def get(self, symbol: str, max_age: float = 30.0) -> float | None:
        """Cached last price, refreshing if stale."""
        symbol = (symbol or "").upper()
        hit = self._cache.get(symbol)
        if hit and time.time() - hit[0] <= max_age:
            return hit[1]
        return self.refresh([symbol]).get(symbol)

    def source_of(self, symbol: str) -> str | None:
        hit = self._cache.get((symbol or "").upper())
        return hit[2] if hit else None

    def age_of(self, symbol: str) -> float | None:
        hit = self._cache.get((symbol or "").upper())
        return time.time() - hit[0] if hit else None

    def _store(self, sym: str, price: float, source: str) -> None:
        self._cache[sym] = (time.time(), price, source)

    def _snapshot(self, symbols: list[str]) -> dict[str, float]:
        """Primary source: md.Market.snapshot().

        Goes through `md.Market` rather than reaching past it to the raw SDK
        data client (which is what this did before the Vesper migration, via
        `wb_data.market_data.get_snapshot` -- an attribute `Market` does not
        expose, so that call broke when this module was restored). Market
        already owns per-call chunking, a short-TTL cache, and the 600/min
        market-data bucket, so routing through it means the watcher inherits
        all of that instead of competing with it. Market normalises rows via
        `_quote()`, whose `last` key `_first_price` below already looks for.
        """
        try:
            rows = self._wb_data.snapshot(symbols, "US_STOCK", extend_hour=True)
        except Exception as e:
            # Entitlement failures are permanent for this process; anything else
            # might be transient, but the distinction is not visible from here,
            # so latch and say so rather than hammer a failing endpoint.
            self._snapshot_dead = True
            self._snapshot_error = str(e)[:120]
            self._log(f"quotes: Webull snapshot unavailable ({self._snapshot_error}); "
                      "falling back to portfolio + TDPro spot")
            return {}

        out: dict[str, float] = {}
        errors: list[str] = []
        for sym, row in (rows or {}).items():
            if not isinstance(row, dict):
                continue
            if row.get("error"):
                errors.append(str(row["error"])[:60])
                continue
            price = _first_price(row)
            if sym and price:
                out[sym.upper()] = price
                self._store(sym.upper(), price, "webull")

        # Market reports per-symbol failures inline rather than raising. If
        # every symbol failed there is no working snapshot source, so latch the
        # same way an outright exception would -- otherwise a permanently
        # unentitled account would retry the full symbol list every tick.
        if errors and not out:
            self._snapshot_dead = True
            self._snapshot_error = errors[0]
            self._log(f"quotes: Webull snapshot unavailable ({self._snapshot_error}); "
                      "falling back to portfolio + TDPro spot")
        return out

    def _from_portfolio(self, symbols: list[str]) -> dict[str, float]:
        if not self._portfolio_fn:
            return {}
        try:
            p = self._portfolio_fn() or {}
        except Exception:
            return {}
        want = set(symbols)
        out: dict[str, float] = {}
        for pos in p.get("positions", []):
            sym = (pos.get("symbol") or "").upper()
            if sym in want:
                price = _f(pos.get("last_price"))
                if price:
                    out[sym] = price
                    self._store(sym, price, "portfolio")
        return out

    def _from_td(self, symbol: str) -> float | None:
        if not self._td:
            return None
        try:
            lv = self._td.levels(symbol)
        except Exception:
            return None
        price = _f((lv or {}).get("spot"))
        if not price:
            return None
        self._store(symbol, price, "tdpro-spot")
        return price


def _f(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _first_price(row: dict) -> float | None:
    """Pull a last price out of a snapshot row.

    The SDK documents the request and not the response, so rather than guess one
    field name and silently read nothing, try the plausible spellings in
    preference order. `close` is Webull's usual name for the last trade; the
    bid/ask midpoint is a deliberate last resort because a wide spread makes it
    a worse trigger than a stale trade.
    """
    for key in ("close", "last", "lastPrice", "price", "tradePrice", "latestPrice", "preClose"):
        v = _f(row.get(key))
        if v:
            return v
    bid, ask = _f(row.get("bid")), _f(row.get("ask"))
    if bid and ask:
        return (bid + ask) / 2
    return None
