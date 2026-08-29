"""watcher.py's _tick() wiring to vesper/metrics.py.

Only the metrics side-effect of a tick is pinned here -- the alert-firing
behaviour itself (crossing properties, pending re-arm) is watcher.py's
caller, alerts.AlertStore.sweep(), already covered by tests/test_alerts.py.
"""

from __future__ import annotations

import watcher as W
from vesper.metrics import metrics


class _StubStore:
    def __init__(self, syms):
        self._syms = syms

    def symbols(self):
        return list(self._syms)

    def sweep(self, price_of, levels_of):
        return []  # no alerts fire; this test is about the metrics call, not delivery


class _StubQuotes:
    """Minimal stand-in for quotes.Quotes -- just enough surface for
    Watcher._tick()."""

    def __init__(self, status):
        self._status = status

    def refresh(self, symbols):
        return {}

    def status(self):
        return self._status

    def get(self, symbol, max_age=30.0):
        return None

    def source_of(self, symbol):
        return None

    def age_of(self, symbol):
        return None


def test_tick_records_a_quote_snapshot_from_quotes_status():
    q = _StubQuotes({"snapshot": "ok", "cached": 2, "sources": {"webull": 2}, "max_age_sec": 3.5})
    w = W.Watcher(store=_StubStore(["SPY"]), quotes=q, levels_of=lambda s: {})
    w._tick()
    snap = metrics.snapshot()["quote_snapshot"]
    assert snap["sources"] == {"webull": 2}
    assert snap["max_age_sec"] == 3.5


def test_tick_with_no_watched_symbols_never_calls_quotes_or_metrics():
    """No alerts armed -> _tick returns before touching quotes.refresh()/
    status() at all, so no quote_snapshot should be recorded."""
    q = _StubQuotes({"snapshot": "ok", "cached": 0, "sources": {}, "max_age_sec": None})
    w = W.Watcher(store=_StubStore([]), quotes=q, levels_of=lambda s: {})
    w._tick()
    snap = metrics.snapshot()["quote_snapshot"]
    assert snap["sources"] == {}
    assert snap["snapshot_count"] == 0


def test_tick_handles_missing_sources_key_defensively():
    """status() missing keys entirely (a stub that predates this change, or a
    degraded source) must not crash the tick -- record_quote_snapshot gets
    an empty dict rather than raising."""
    q = _StubQuotes({"snapshot": "unavailable", "cached": 0})
    w = W.Watcher(store=_StubStore(["SPY"]), quotes=q, levels_of=lambda s: {})
    w._tick()  # must not raise
    snap = metrics.snapshot()["quote_snapshot"]
    assert snap["sources"] == {}
    assert snap["max_age_sec"] is None
