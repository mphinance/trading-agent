"""Quote source chain: Webull market data -> portfolio poll -> TDPro spot.

The order matters and so does the degradation. Market data is separately
entitled from trading and may simply refuse, and an alert fired off a
five-minute-old TDPro print is a different claim from one fired off a live
quote — so every price carries its source.
"""

from __future__ import annotations

import pytest

import quotes as Q


# These fakes stand in for `md.Market`, which is what Quotes takes as
# `wb_data`. They used to mimic the raw SDK data client
# (`.market_data.get_snapshot()` returning an HTTP response), because that is
# what quotes.py called before the Vesper migration deleted it. On restore,
# quotes.py was pointed at `Market.snapshot()` instead -- Market owns the
# chunking, caching and 600/min bucket handling, so the watcher should share
# that rather than bypass it -- and these fakes follow.


class DeadSnapshot:
    """A market-data client that is not entitled."""

    @staticmethod
    def snapshot(symbols, category="US_STOCK", extend_hour=True):
        raise RuntimeError("403 not entitled")


class OkSnapshot:
    """Market.snapshot() returns {symbol: normalised_row}, and the normalised
    row's last-trade key is `last` (see md._quote)."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def snapshot(self, symbols, category="US_STOCK", extend_hour=True):
        self.calls.append(",".join(symbols))
        out = {}
        for row in self._rows:
            sym = row.get("symbol")
            if sym:
                out[sym] = row
        return out


class ErroringSnapshot:
    """Market reports per-symbol failures inline as {"error": ...} rather than
    raising. Every symbol failing must latch the source off exactly like an
    outright exception does."""

    @staticmethod
    def snapshot(symbols, category="US_STOCK", extend_hour=True):
        return {s: {"symbol": s, "error": "403 not entitled"} for s in symbols}


PORTFOLIO = {"positions": [{"symbol": "SPY", "last_price": 746.5}]}


def td_stub(spot=61.2):
    return type("TD", (), {"levels": lambda self, s: {"spot": spot}})()


# --------------------------------------------------------------------------
# Fallback chain
# --------------------------------------------------------------------------

def test_falls_through_to_portfolio_then_tdpro_when_snapshot_refuses():
    q = Q.Quotes(wb_data=DeadSnapshot(), portfolio_fn=lambda: PORTFOLIO, td=td_stub())
    got = q.refresh(["SPY", "GLD"])
    assert got["SPY"] == 746.5 and got["GLD"] == 61.2
    assert q.source_of("SPY") == "portfolio"
    assert q.source_of("GLD") == "tdpro-spot"


def test_snapshot_wins_when_it_works():
    q = Q.Quotes(wb_data=OkSnapshot([{"symbol": "SPY", "close": 747.1}]),
                 portfolio_fn=lambda: PORTFOLIO, td=td_stub())
    assert q.refresh(["SPY"])["SPY"] == 747.1
    assert q.source_of("SPY") == "webull"


def test_a_dead_snapshot_latches_off_instead_of_retrying_every_tick():
    logs = []
    q = Q.Quotes(wb_data=DeadSnapshot(), portfolio_fn=lambda: PORTFOLIO,
                 td=td_stub(), on_log=logs.append)
    q.refresh(["SPY"])
    assert q.snapshot_available is False
    before = len(logs)
    for _ in range(5):
        q.refresh(["SPY"])
    assert len(logs) == before, "must not hammer an endpoint that already refused"


def test_set_data_client_clears_the_latch():
    q = Q.Quotes(wb_data=DeadSnapshot(), portfolio_fn=lambda: PORTFOLIO, td=td_stub())
    q.refresh(["SPY"])
    assert q.snapshot_available is False
    q.set_data_client(OkSnapshot([{"symbol": "SPY", "close": 1.0}]))
    assert q.snapshot_available is True


def test_status_reports_why_the_snapshot_is_unavailable():
    q = Q.Quotes(wb_data=DeadSnapshot(), portfolio_fn=lambda: PORTFOLIO, td=td_stub())
    q.refresh(["SPY"])
    assert "entitled" in q.status()["snapshot"]


def test_no_sources_at_all_returns_nothing_rather_than_raising():
    q = Q.Quotes()
    assert q.refresh(["SPY"]) == {}
    assert q.get("SPY") is None


def test_a_broken_portfolio_fn_does_not_propagate():
    def boom():
        raise RuntimeError("429")
    q = Q.Quotes(portfolio_fn=boom, td=td_stub())
    assert q.refresh(["SPY"])["SPY"] == 61.2, "should fall through to TDPro, not raise"


def test_portfolio_only_answers_for_held_symbols():
    q = Q.Quotes(portfolio_fn=lambda: PORTFOLIO)
    assert q.refresh(["SPY", "GLD"]) == {"SPY": 746.5}


# --------------------------------------------------------------------------
# Batching and caching
# --------------------------------------------------------------------------

def test_requests_are_chunked_to_the_batch_ceiling():
    client = OkSnapshot([])
    q = Q.Quotes(wb_data=client)
    q.refresh([f"S{i}" for i in range(Q.MAX_BATCH + 5)])
    assert len(client.calls) == 2, "an opaque 400 at symbol 101 is not a good failure mode"


def test_get_serves_from_cache_within_max_age():
    client = OkSnapshot([{"symbol": "SPY", "close": 100.0}])
    q = Q.Quotes(wb_data=client)
    q.refresh(["SPY"])
    calls = len(client.calls)
    assert q.get("SPY", max_age=999) == 100.0
    assert len(client.calls) == calls, "a fresh cache hit must not refetch"


def test_get_refetches_once_stale():
    client = OkSnapshot([{"symbol": "SPY", "close": 100.0}])
    q = Q.Quotes(wb_data=client)
    q.refresh(["SPY"])
    calls = len(client.calls)
    q.get("SPY", max_age=-1)
    assert len(client.calls) > calls


# --------------------------------------------------------------------------
# Price extraction. The SDK documents the request and not the response.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("row,expected", [
    ({"symbol": "X", "close": "10.5"}, 10.5),
    ({"symbol": "X", "last": 9}, 9.0),
    ({"symbol": "X", "lastPrice": 3.25}, 3.25),
    ({"symbol": "X", "tradePrice": 7}, 7.0),
    ({"symbol": "X", "bid": 10, "ask": 11}, 10.5),
    ({"symbol": "X", "volume": 5}, None),
    ({"symbol": "X"}, None),
    ({"symbol": "X", "close": 0}, None),
    ({"symbol": "X", "close": "not a number"}, None),
])
def test_price_field_spellings(row, expected):
    assert Q._first_price(row) == expected


def test_close_is_preferred_over_the_bid_ask_midpoint():
    """A wide spread makes the midpoint a worse trigger than a real trade."""
    assert Q._first_price({"symbol": "X", "close": 10.0, "bid": 1, "ask": 100}) == 10.0


def test_inline_per_symbol_errors_latch_the_source_off():
    """Regression for the restore: md.Market reports failures inline instead of
    raising, so a purely-inline failure must still latch snapshot_available
    off. Without this, a permanently unentitled account would rebuild the full
    symbol list and re-call every single tick forever."""
    logs = []
    q = Q.Quotes(wb_data=ErroringSnapshot(), portfolio_fn=lambda: PORTFOLIO,
                 td=td_stub(), on_log=logs.append)
    got = q.refresh(["SPY"])

    assert q.snapshot_available is False
    assert "entitled" in q.status()["snapshot"]
    # and it still degraded correctly rather than returning nothing
    assert got["SPY"] == 746.5
    assert q.source_of("SPY") == "portfolio"


def test_partial_inline_errors_do_not_latch_when_some_symbols_work():
    """The inverse: one bad symbol must not disable the source for the others."""
    class Partial:
        @staticmethod
        def snapshot(symbols, category="US_STOCK", extend_hour=True):
            return {"SPY": {"symbol": "SPY", "last": 747.1},
                    "BADSYM": {"symbol": "BADSYM", "error": "unknown symbol"}}

    q = Q.Quotes(wb_data=Partial(), portfolio_fn=lambda: PORTFOLIO, td=td_stub())
    got = q.refresh(["SPY", "BADSYM"])
    assert got["SPY"] == 747.1
    assert q.source_of("SPY") == "webull"
    assert q.snapshot_available is True, "one bad symbol must not kill the source"
