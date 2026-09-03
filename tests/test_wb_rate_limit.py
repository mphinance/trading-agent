"""M3-06: rate-limit discipline is preserved on the remote box.

core/wb.py's Webull.portfolio() is the only path that reads the account
endpoint -- balance and positions -- which Webull caps at 2 req/2s (see
core/wb.py's module docstring). It protects that budget with two mechanisms
stacked together: `with self._lock:` serializes every caller, and
`_cached("portfolio", self._portfolio_uncached)` (CACHE_TTL_SEC=8.0) means
whoever gets there first does the real fetch and everyone else -- inside the
lock, so strictly after the first caller finishes -- reads the memoized
result instead of re-hitting the SDK.

No existing test exercises this pair directly. Every other test that touches
`core.wb.Webull` mocks the class wholesale (`mock_wb.portfolio.return_value =
...`), which proves the caller-side code reacts correctly to a portfolio
dict, not that a burst of real callers against a real Webull instance would
stay inside the account bucket instead of 429ing it. That is the property
this file pins.

Fake construction follows tests/test_wb_credential_logging.py's and
tests/test_metrics.py's `_build_webull` pattern: monkeypatch the SDK classes
at the wb module level. The real `webull` package is never installed in CI
(already stubbed to bare `object` in conftest.py -- see its module
docstring), so this never touches the network or a real SDK.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import core.wb as wb


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _CountingAccountV2:
    """Stands in for TradeClient.account_v2. Every call increments a
    thread-safe counter so a test can assert exactly how many times the
    underlying SDK -- the thing Webull's 2 req/2s bucket actually meters --
    was hit, independent of how many application-level callers asked."""

    def __init__(self) -> None:
        self._count_lock = threading.Lock()
        self.calls: dict[str, int] = {
            "get_account_list": 0,
            "get_account_balance": 0,
            "get_account_position": 0,
        }

    def _bump(self, name: str) -> None:
        with self._count_lock:
            self.calls[name] += 1

    def get_account_list(self):
        self._bump("get_account_list")
        return _FakeResponse(
            [{"account_id": "ACC1", "account_label": "Individual", "account_class": "CASH"}]
        )

    def get_account_balance(self, account_id):
        self._bump("get_account_balance")
        # A small delay widens the race window a missing lock would need to
        # actually double-fetch -- without it, ten threads could happen to
        # serialize on a fast machine anyway and the test would pass for the
        # wrong reason (no real contention exercised).
        time.sleep(0.02)
        return _FakeResponse(
            {
                "account_currency_assets": [
                    {
                        "buying_power": "1000.0",
                        "option_buying_power": "500.0",
                        "night_trading_buying_power": "1000.0",
                    }
                ],
                "total_net_liquidation_value": "10000.0",
                "total_cash_balance": "5000.0",
                "total_market_value": "5000.0",
                "total_day_profit_loss": "10.0",
                "total_unrealized_profit_loss": "100.0",
            }
        )

    def get_account_position(self, account_id):
        self._bump("get_account_position")
        time.sleep(0.02)
        return _FakeResponse([])


def _build_webull(monkeypatch):
    account_v2 = _CountingAccountV2()
    fake_trade = type("FakeTrade", (), {"account_v2": account_v2})()

    monkeypatch.setattr(
        wb, "ApiClient", lambda *a, **k: type("C", (), {"add_endpoint": lambda *a, **k: None})()
    )
    monkeypatch.setattr(wb, "TradeClient", lambda c: fake_trade)
    monkeypatch.setattr(wb, "DataClient", lambda c: object())
    monkeypatch.setattr(wb, "credentials", lambda: ("key", "secret", "us"))
    w = wb.Webull()
    return w, account_v2


def test_concurrent_portfolio_burst_hits_broker_exactly_once(monkeypatch):
    """The whole point of the lock + TTL cache: N concurrent callers must
    collapse to ONE real get_account_balance / get_account_position call
    each, not N. Each endpoint is capped at 2 req/2s -- ten concurrent
    callers (e.g. the monitor loop, a status check, and a UI poll landing at
    once) each firing their own request would blow straight through that and
    draw a 429."""
    w, account_v2 = _build_webull(monkeypatch)

    n_callers = 10
    barrier = threading.Barrier(n_callers)

    def _call(_):
        barrier.wait(timeout=5)  # line every caller up so they hit the lock together
        return w.portfolio()

    with ThreadPoolExecutor(max_workers=n_callers) as pool:
        results = list(pool.map(_call, range(n_callers)))

    assert account_v2.calls["get_account_balance"] == 1
    assert account_v2.calls["get_account_position"] == 1
    assert account_v2.calls["get_account_list"] == 1

    # Every caller gets back the one real snapshot, not a mix of partial ones.
    assert len(results) == n_callers
    for r in results:
        assert r["stale"] is False
        assert r["accounts"][0]["account_id"] == "ACC1"
        assert r["totals"]["nlv"] == 10000.0


def test_sequential_rapid_calls_also_stay_within_the_cache_ttl(monkeypatch):
    """No threads at all -- a tight synchronous loop, the shape a single
    poller (CLI status checked twice in a row, the monitor's own retry)
    actually produces -- must also collapse to one fetch, since a loop like
    this finishes in milliseconds, comfortably inside CACHE_TTL_SEC (8s)."""
    w, account_v2 = _build_webull(monkeypatch)

    for _ in range(5):
        w.portfolio()

    assert account_v2.calls["get_account_balance"] == 1
    assert account_v2.calls["get_account_position"] == 1


def test_cache_expiry_allows_a_genuine_fresh_fetch_after_ttl(monkeypatch):
    """Guards against the other failure direction: the cache must actually
    expire, not latch forever. Without this, the two tests above would also
    pass for a `portfolio()` that simply never refetches -- which would hide
    stale balances from the account forever, not protect the rate limit.

    The virtual clock (monkeypatching time.monotonic rather than sleeping
    the real 8s) is required because CACHE_TTL_SEC is bound into `_cached`'s
    default argument at wb.py's import time; patching the module-level
    `wb.CACHE_TTL_SEC` name afterwards would not reach it.
    """
    w, account_v2 = _build_webull(monkeypatch)
    virtual_now = {"t": 0.0}
    monkeypatch.setattr(wb.time, "monotonic", lambda: virtual_now["t"])

    w.portfolio()
    assert account_v2.calls["get_account_balance"] == 1

    virtual_now["t"] += wb.CACHE_TTL_SEC + 1.0
    w.portfolio()
    assert account_v2.calls["get_account_balance"] == 2


def test_wb_and_md_remain_separate_rate_limit_modules():
    """M3-06's other sub-requirement: core/wb.py (the scarce 2 req/2s account
    bucket) and core/md.py (the generous 600/min market-data bucket) must
    stay genuinely separate modules -- merging them would let a burst of
    market-data reads share, and exhaust, the account endpoint's budget. See
    both modules' docstrings, and CLAUDE.md's "Do not merge those two
    modules" gotcha."""
    import core.md as md

    assert wb is not md
    assert wb.__file__ != md.__file__
    # Each module owns its own pacing/TTL constants rather than importing
    # wb's -- a shared constant would be a soft sign the budgets got merged.
    assert not hasattr(md, "PACE_SEC")
    assert not hasattr(md, "CACHE_TTL_SEC")
    assert md.QUOTE_TTL_SEC != wb.CACHE_TTL_SEC
