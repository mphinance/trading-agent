"""Market data, research, screeners and watchlists.

Everything here rides Webull's *market data* rate limit — 600 req/min in the US
region — which is a different bucket from the 2 req/2s that balance, positions
and order queries share. That distinction is the whole reason this module is
separate from `wb.py`: quotes can refresh every second without spending any of
the account budget, and a slow research call can never starve the portfolio poll.

All of it is read-only. Watchlists are the one exception — they write, but they
write to a list of tickers, not to the account.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from webull.data.common.category import Category

from core.metrics import metrics

# Snapshots are cheap; a 1s TTL still leaves ~540 req/min of headroom for
# research calls even if the UI polls hard.
QUOTE_TTL_SEC = 1.0
BAR_TTL_SEC = 30.0
RESEARCH_TTL_SEC = 900.0  # fundamentals and profiles barely move intraday
SCREENER_TTL_SEC = 60.0
CHAIN_TTL_SEC = 60.0

# Batch snapshot calls take a comma-joined symbol string. Keep requests under a
# sane width so one bad ticker doesn't fail the whole basket.
MAX_SYMBOLS_PER_CALL = 50


class MarketDataError(RuntimeError):
    pass


def category_for(instrument_type: str) -> str:
    """Map a position's instrument_type onto a market-data Category name."""
    t = (instrument_type or "").upper()
    if t in ("OPTION", "CALL_OPTION", "PUT_OPTION"):
        return Category.US_OPTION.name
    if t == "CRYPTO":
        return Category.US_CRYPTO.name
    if t == "FUTURES":
        return Category.US_FUTURES.name
    if t == "EVENT":
        return Category.US_EVENT.name
    if t == "ETF":
        return Category.US_ETF.name
    return Category.US_STOCK.name


class Market:
    """Read-only market data over the shared ApiClient.

    Construct with a `Webull` instance so we reuse its authenticated client
    rather than logging in twice (two clients means two token files and two
    2FA states, which desynchronise in exactly the way you'd expect).
    """

    def __init__(self, webull) -> None:
        self._wb = webull
        self._d = webull.data
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _cached(self, key: str, fn, ttl: float):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        with self._lock:
            # Re-check inside the lock: several UI panels can ask for the same
            # symbol in the same tick, and only one of them should pay for it.
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            # Every method on this class funnels through here on a cache
            # miss, so this is the one place to time+count the whole
            # "market_data" bucket generically rather than instrumenting
            # each method separately. endpoint is the key's prefix before
            # its first ":" (e.g. "snap", "l2", "tick", "scr", "wl") --
            # every cache key in this module is built that way.
            endpoint = key.split(":", 1)[0]
            start = time.monotonic()
            try:
                val = fn()
            except Exception:
                metrics.record_broker_call("market_data", endpoint, time.monotonic() - start, ok=False)
                raise
            metrics.record_broker_call("market_data", endpoint, time.monotonic() - start, ok=True)
            self._cache[key] = (time.monotonic(), val)
            return val

    @staticmethod
    def _json(res) -> Any:
        if getattr(res, "status_code", 200) != 200:
            raise MarketDataError(f"HTTP {res.status_code}: {getattr(res, 'text', '')[:200]}")
        return res.json()

    # -- quotes -----------------------------------------------------------

    def snapshot(self, symbols: list[str], category: str = "US_STOCK",
                 extend_hour: bool = True) -> dict[str, dict]:
        """Live quote per symbol, keyed by symbol.

        `extend_hour` on by default — you trade beside Webull Desktop, which
        shows pre/post prints; a deck that silently drops to the 4pm close while
        the desktop keeps ticking is worse than no quote at all.
        """
        symbols = [s for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), MAX_SYMBOLS_PER_CALL):
            chunk = symbols[i:i + MAX_SYMBOLS_PER_CALL]
            key = f"snap:{category}:{','.join(chunk)}:{extend_hour}"
            try:
                rows = self._cached(
                    key,
                    lambda c=chunk: self._json(
                        self._d.market_data.get_snapshot(
                            ",".join(c), category, extend_hour_required=extend_hour
                        )
                    ),
                    QUOTE_TTL_SEC,
                )
            except Exception as e:
                for s in chunk:
                    out[s] = {"symbol": s, "error": str(e)}
                continue
            for row in _rows(rows):
                sym = row.get("symbol")
                if sym:
                    out[sym] = _quote(row)
        return out

    def quotes(self, symbol: str, category: str = "US_STOCK", depth: int = 5) -> Any:
        """Level 2 order book for one symbol."""
        return self._cached(
            f"l2:{category}:{symbol}:{depth}",
            lambda: self._json(self._d.market_data.get_quotes(symbol, category, depth=depth)),
            QUOTE_TTL_SEC,
        )

    def tick(self, symbol: str, category: str = "US_STOCK", count: int = 50) -> Any:
        """Time & sales."""
        return self._cached(
            f"tick:{category}:{symbol}:{count}",
            lambda: self._json(self._d.market_data.get_tick(symbol, category, count=str(count))),
            QUOTE_TTL_SEC,
        )

    def bars(self, symbol: str, category: str = "US_STOCK", timespan: str = "M5",
             count: int = 120, sessions: str | None = None) -> Any:
        """OHLCV history — the sparkline/chart feed."""
        return self._cached(
            f"bar:{category}:{symbol}:{timespan}:{count}:{sessions}",
            lambda: self._json(
                self._d.market_data.get_history_bar(
                    symbol, category, timespan, count=str(count),
                    real_time_required=True, trading_sessions=sessions,
                )
            ),
            BAR_TTL_SEC,
        )

    def batch_bars(self, symbols: list[str], category: str = "US_STOCK",
                   timespan: str = "M5", count: int = 60) -> Any:
        syms = ",".join(dict.fromkeys(s for s in symbols if s))
        if not syms:
            return []
        return self._cached(
            f"bbar:{category}:{syms}:{timespan}:{count}",
            lambda: self._json(
                self._d.market_data.get_batch_history_bar(
                    syms, category, timespan, count=str(count), real_time_required=True
                )
            ),
            BAR_TTL_SEC,
        )

    def footprint(self, symbols: list[str], category: str = "US_STOCK",
                  timespan: str = "M5", count: int = 30) -> Any:
        """Bid/ask volume split per price level — order-flow read."""
        syms = ",".join(s for s in symbols if s)
        return self._cached(
            f"fp:{category}:{syms}:{timespan}:{count}",
            lambda: self._json(
                self._d.market_data.get_footprint(syms, category, timespan, count=count)
            ),
            BAR_TTL_SEC,
        )

    def noii(self, symbol: str, action_type: str = "ALL") -> Any:
        """NASDAQ auction imbalance. Only publishes during the call auction windows."""
        return self._cached(
            f"noii:{symbol}:{action_type}",
            lambda: self._json(
                self._d.market_data.get_noii_snapshot(symbol, Category.US_STOCK.name, action_type)
            ),
            5.0,
        )

    # -- options ----------------------------------------------------------

    def option_chain(self, underlying: str, expire_date: str | None = None,
                     option_type: str | None = None, strike_gte: float | None = None,
                     strike_lte: float | None = None, page_size: int = 200) -> Any:
        """Option contracts for an underlying.

        Note the SDK's parameter names: an exact expiry is `start_date`, not
        `expire_date` — `end_date` is a lower bound on expiry, not an upper one.
        Both read backwards, so they're mapped here rather than at each call site.
        """
        key = f"chain:{underlying}:{expire_date}:{option_type}:{strike_gte}:{strike_lte}:{page_size}"
        return self._cached(
            key,
            lambda: self._json(
                self._d.instrument.get_option_contracts(
                    Category.US_OPTION.name,
                    underlying_symbols=underlying,
                    start_date=expire_date,
                    option_type=option_type,
                    strike_price_gte=strike_gte,
                    strike_price_lte=strike_lte,
                    page_size=page_size,
                )
            ),
            CHAIN_TTL_SEC,
        )

    def option_snapshot(self, symbols: list[str]) -> Any:
        syms = ",".join(s for s in symbols if s)
        if not syms:
            return []
        return self._cached(
            f"osnap:{syms}",
            lambda: self._json(
                self._d.option_market_data.get_option_snapshot(syms, Category.US_OPTION.name)
            ),
            QUOTE_TTL_SEC,
        )

    def option_bars(self, symbols: list[str], timespan: str = "M5", count: int = 60) -> Any:
        syms = ",".join(s for s in symbols if s)
        return self._cached(
            f"obar:{syms}:{timespan}:{count}",
            lambda: self._json(
                self._d.option_market_data.get_option_history_bars(
                    syms, Category.US_OPTION.name, timespan, count=str(count)
                )
            ),
            BAR_TTL_SEC,
        )

    # -- crypto / futures / events ---------------------------------------

    def crypto_snapshot(self, symbols: list[str]) -> Any:
        syms = ",".join(s for s in symbols if s)
        return self._cached(
            f"csnap:{syms}",
            lambda: self._json(self._d.crypto_market_data.get_crypto_snapshot(syms)),
            QUOTE_TTL_SEC,
        )

    def futures_snapshot(self, symbols: list[str]) -> Any:
        syms = ",".join(s for s in symbols if s)
        return self._cached(
            f"fsnap:{syms}",
            lambda: self._json(
                self._d.futures_market_data.get_futures_snapshot(syms, Category.US_FUTURES.name)
            ),
            QUOTE_TTL_SEC,
        )

    def event_snapshot(self, symbols: list[str]) -> Any:
        syms = ",".join(s for s in symbols if s)
        return self._cached(
            f"esnap:{syms}",
            lambda: self._json(self._d.event_market_data.get_event_snapshot(syms)),
            QUOTE_TTL_SEC,
        )

    # -- research ---------------------------------------------------------

    def profile(self, symbol: str) -> Any:
        return self._research(f"profile:{symbol}", lambda: self._d.instrument.get_company_profile(symbol))

    def analyst_rating(self, symbol: str) -> Any:
        return self._research(f"rating:{symbol}", lambda: self._d.instrument.get_analyst_rating(symbol))

    def analyst_target(self, symbol: str) -> Any:
        return self._research(f"target:{symbol}", lambda: self._d.instrument.get_analyst_target_price(symbol))

    def capital_flow(self, symbol: str) -> Any:
        return self._research(f"flow:{symbol}", lambda: self._d.fundamentals.get_capital_flow(symbol), ttl=120.0)

    def earnings_calendar(self, symbol: str) -> Any:
        return self._research(f"earn:{symbol}", lambda: self._d.fundamentals.get_earnings_calendar(symbol))

    def dividend_calendar(self, symbol: str) -> Any:
        return self._research(f"div:{symbol}", lambda: self._d.fundamentals.get_dividend_calendar(symbol))

    def sec_filings(self, symbol: str) -> Any:
        return self._research(f"sec:{symbol}", lambda: self._d.fundamentals.get_sec_filings(symbol))

    def forecast_eps(self, symbol: str) -> Any:
        return self._research(f"eps:{symbol}", lambda: self._d.fundamentals.get_forecast_eps(symbol))

    def industry_comparison(self, symbol: str) -> Any:
        return self._research(f"peer:{symbol}", lambda: self._d.fundamentals.get_industry_comparison(symbol))

    def financials(self, symbol: str, statement: str = "indicators", count: int = 4) -> Any:
        """statement: indicators | income | cashflow | balance | alert"""
        fn = {
            "indicators": self._d.fundamentals.get_financials_indicators,
            "income": self._d.fundamentals.get_financials_income,
            "cashflow": self._d.fundamentals.get_financials_cashflow,
            "balance": self._d.fundamentals.get_financials_balance_sheet,
        }.get(statement)
        if fn is None:
            if statement == "alert":
                return self._research(f"fin:alert:{symbol}",
                                      lambda: self._d.fundamentals.get_financials_alert(symbol))
            raise MarketDataError(f"unknown statement: {statement}")
        return self._research(f"fin:{statement}:{symbol}:{count}", lambda: fn(symbol, count=count))

    def _research(self, key: str, call, ttl: float = RESEARCH_TTL_SEC) -> Any:
        return self._cached(key, lambda: self._json(call()), ttl)

    # -- screener ---------------------------------------------------------

    def screener(self, kind: str, category: str = "US_STOCK", page_size: int = 20,
                 rank_type: str | None = None, sort_by: str | None = None) -> Any:
        """kind: gainers | losers | active | sectors | dividend | 52whl"""
        s = self._d.screener
        if kind in ("gainers", "losers"):
            rt = rank_type or ("GAINERS" if kind == "gainers" else "LOSERS")
            call = lambda: s.get_gainers_losers(rt, category, sort_by or "CHANGE_RATIO",
                                                page_size=page_size)
        elif kind == "active":
            call = lambda: s.get_most_active(category, rank_type=rank_type, sort_by=sort_by,
                                             page_size=page_size)
        elif kind == "sectors":
            call = lambda: s.get_market_sectors(category, page_size=page_size)
        elif kind == "dividend":
            call = lambda: s.get_high_dividend(category, sort_by=sort_by, page_size=page_size)
        elif kind == "52whl":
            call = lambda: s.get_52whl(category, rank_type=rank_type, sort_by=sort_by,
                                       page_size=page_size)
        else:
            raise MarketDataError(f"unknown screener: {kind}")
        return self._cached(f"scr:{kind}:{category}:{rank_type}:{sort_by}:{page_size}",
                            lambda: self._json(call()), SCREENER_TTL_SEC)

    # -- watchlist --------------------------------------------------------
    # Writes, but only to a list of tickers — no account impact.

    def watchlists(self) -> Any:
        return self._cached("wl", lambda: self._json(self._d.watchlist.get_watchlist()), 30.0)

    def watchlist_items(self, watchlist_id: str) -> Any:
        return self._cached(f"wl:{watchlist_id}",
                            lambda: self._json(self._d.watchlist.get_instruments(watchlist_id)), 15.0)

    def watchlist_create(self, name: str) -> Any:
        self._cache.pop("wl", None)
        return self._json(self._d.watchlist.create_watchlist(name))

    def watchlist_delete(self, watchlist_id: str) -> Any:
        self._cache.pop("wl", None)
        return self._json(self._d.watchlist.delete_watchlist(watchlist_id))

    def watchlist_add(self, watchlist_id: str, instruments: list[dict]) -> Any:
        self._cache.pop(f"wl:{watchlist_id}", None)
        return self._json(self._d.watchlist.add_instruments(watchlist_id, instruments))

    def watchlist_remove(self, watchlist_id: str, instruments: list[dict]) -> Any:
        self._cache.pop(f"wl:{watchlist_id}", None)
        return self._json(self._d.watchlist.remove_instruments(watchlist_id, instruments))

    # -- instrument lookup ------------------------------------------------

    def instrument(self, symbols: str, category: str = "US_STOCK") -> Any:
        return self._cached(f"inst:{category}:{symbols}",
                            lambda: self._json(self._d.instrument.get_instrument(symbols, category)),
                            RESEARCH_TTL_SEC)


def _rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("data", "items", "snapshots"):
            v = payload.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        if payload.get("symbol"):
            return [payload]
    return []


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _quote(row: dict) -> dict:
    """Normalise a snapshot row.

    Webull returns most of this as strings, and the field names drift between
    asset classes (`close` vs `last_price` vs `trade_price`), so coalesce rather
    than trust one key.
    """
    last = _num(row.get("last_price") or row.get("close") or row.get("trade_price"))
    prev = _num(row.get("pre_close") or row.get("previous_close"))
    change = _num(row.get("change")) or (last - prev if prev else 0.0)
    return {
        "symbol": row.get("symbol", "?"),
        "last": last,
        "prev_close": prev,
        "open": _num(row.get("open")),
        "high": _num(row.get("high")),
        "low": _num(row.get("low")),
        "volume": _num(row.get("volume")),
        "change": change,
        "change_pct": _num(row.get("change_ratio")) or ((change / prev) if prev else 0.0),
        "bid": _num(row.get("bid_price")),
        "ask": _num(row.get("ask_price")),
        "bid_size": _num(row.get("bid_size")),
        "ask_size": _num(row.get("ask_size")),
        "status": row.get("trade_status") or row.get("status") or "",
        "timestamp": row.get("trade_time") or row.get("timestamp") or "",
    }
