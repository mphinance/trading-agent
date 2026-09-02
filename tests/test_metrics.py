"""Unit tests for vesper/metrics.py -- the Health/observability metrics module.

Covers the working path (counters/timings accumulate and aggregate
correctly), the bounding discipline (rolling windows stay bounded), the
missing/degraded path (empty series, missing/corrupt snapshot file), the
redaction rule enforced by signature shape, and a small integration check
that mirrors how runner.py/monitor.py actually call this module.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from vesper.metrics import (
    Metrics,
    bucket_approval_ages,
    metrics as metrics_singleton,
    read_snapshot,
    write_snapshot,
)


@pytest.fixture
def m():
    return Metrics()


# -- record_broker_call ---------------------------------------------------


def test_broker_call_counts_ok_and_error(m):
    m.record_broker_call("account", "get_account_balance", 0.1, ok=True)
    m.record_broker_call("account", "get_account_balance", 0.2, ok=False)
    m.record_broker_call("account", "get_account_balance", 0.1, ok=False, rate_limited=True)
    snap = m.snapshot()
    entry = snap["broker_calls"]["account"]["get_account_balance"]
    assert entry["ok"] == 1
    assert entry["error"] == 2
    assert entry["rate_limited"] == 1
    assert entry["count"] == 3
    assert entry["p50_ms"] is not None


def test_broker_call_buckets_are_independent(m):
    m.record_broker_call("account", "get_account_balance", 0.1, ok=True)
    m.record_broker_call("market_data", "snap", 0.05, ok=True)
    snap = m.snapshot()
    assert "get_account_balance" in snap["broker_calls"]["account"]
    assert "snap" in snap["broker_calls"]["market_data"]
    assert "snap" not in snap["broker_calls"]["account"]


def test_broker_call_duration_window_is_bounded(m):
    for i in range(250):
        m.record_broker_call("account", "get_account_balance", 0.001 * i, ok=True)
    snap = m.snapshot()
    entry = snap["broker_calls"]["account"]["get_account_balance"]
    assert entry["ok"] == 250          # counters are unbounded ints
    assert entry["count"] == 200       # but the duration window is capped


def test_broker_call_no_data_reports_none_not_zero(m):
    """Missing-data path: an empty series must read as None, never a
    fabricated 0ms."""
    snap = m.snapshot()
    assert snap["broker_calls"] == {}


# -- record_llm_call --------------------------------------------------------


def test_llm_call_outcomes_are_counted_per_tier(m):
    m.record_llm_call("deepseek/deepseek-v4-flash", 0.5, outcome="ok")
    m.record_llm_call("deepseek/deepseek-v4-flash", 0.1, outcome="http_error")
    m.record_llm_call("deepseek/deepseek-v4-pro", 1.2, outcome="ok")
    snap = m.snapshot()
    flash = snap["llm_calls"]["deepseek/deepseek-v4-flash"]
    assert flash["ok"] == 1
    assert flash["http_error"] == 1
    pro = snap["llm_calls"]["deepseek/deepseek-v4-pro"]
    assert pro is not flash  # tiers are independent buckets, not shared/leaked
    assert pro["ok"] == 1
    assert pro.get("http_error", 0) == 0


def test_llm_call_disabled_is_distinguishable_from_failed_attempt(m):
    m.record_llm_call("deepseek/deepseek-v4-flash", 0.0, outcome="disabled")
    m.record_llm_call("deepseek/deepseek-v4-flash", 0.3, outcome="timeout_or_network")
    snap = m.snapshot()["llm_calls"]["deepseek/deepseek-v4-flash"]
    assert snap["disabled"] == 1
    assert snap["timeout_or_network"] == 1


# -- record_tool_rejection ---------------------------------------------------


def test_tool_rejection_accumulates_across_passes(m):
    m.record_tool_rejection("risk_gate_node", passed=2, rejected=1)
    m.record_tool_rejection("risk_gate_node", passed=3, rejected=0)
    snap = m.snapshot()["tool_rejections"]["risk_gate_node"]
    assert snap == {"passed": 5, "rejected": 1}


# -- record_order_outcome ----------------------------------------------------


def test_order_outcome_counts_by_mode_status_broker(m):
    m.record_order_outcome(mode="paper", status="DRY_RUN_SIMULATED", broker="webull", payload_digest="abc123")
    m.record_order_outcome(mode="paper", status="DRY_RUN_SIMULATED", broker="webull", payload_digest="def456")
    m.record_order_outcome(mode="live", status="BLOCKED_BY_GUARDRAIL", broker="webull", payload_digest="ghi789")
    snap = m.snapshot()["order_outcomes"]
    assert snap["paper"]["webull"]["DRY_RUN_SIMULATED"] == 2
    assert snap["live"]["webull"]["BLOCKED_BY_GUARDRAIL"] == 1


def test_order_outcome_recent_digests_are_bounded_and_never_raw_payload(m):
    for i in range(75):
        m.record_order_outcome(mode="paper", status="SUBMITTED", broker="webull", payload_digest=f"digest-{i}")
    snap = m.snapshot()
    recent = snap["recent_order_digests"]
    assert len(recent) == 50  # bounded, matches _RECENT_ORDERS_MAXLEN
    assert snap["order_outcomes"]["paper"]["webull"]["SUBMITTED"] == 75  # counter itself unbounded
    for entry in recent:
        assert set(entry) == {"mode", "status", "broker", "payload_digest", "at"}
        assert "symbol" not in entry and "price" not in entry and "quantity" not in entry


# -- record_quote_snapshot ---------------------------------------------------


def test_quote_snapshot_is_latest_reading_only(m):
    m.record_quote_snapshot({"webull": 5}, max_age_sec=1.0)
    m.record_quote_snapshot({"webull": 3, "portfolio": 2}, max_age_sec=4.5)
    snap = m.snapshot()["quote_snapshot"]
    assert snap["sources"] == {"webull": 3, "portfolio": 2}
    assert snap["max_age_sec"] == 4.5
    assert snap["snapshot_count"] == 2  # the counter, unlike the reading, does accumulate


def test_quote_snapshot_absent_reports_empty_not_fabricated(m):
    snap = m.snapshot()["quote_snapshot"]
    assert snap["sources"] == {}
    assert snap["max_age_sec"] is None


# -- snapshot() is JSON-serializable ------------------------------------------


def test_snapshot_round_trips_through_json(m):
    m.record_broker_call("account", "get_account_balance", 0.1, ok=True)
    m.record_llm_call("deepseek/deepseek-v4-flash", 0.2, outcome="ok")
    m.record_tool_rejection("risk_gate_node", passed=1, rejected=1)
    m.record_order_outcome(mode="paper", status="SUBMITTED", broker="webull", payload_digest="abc")
    m.record_quote_snapshot({"webull": 1}, max_age_sec=0.5)
    json.dumps(m.snapshot())  # must not raise


# -- redaction, enforced by signature shape -----------------------------------


RECORD_METHODS = [
    getattr(Metrics, name) for name in dir(Metrics)
    if name.startswith("record_") and callable(getattr(Metrics, name))
]


def test_no_record_method_takes_a_bare_dict_payload():
    """Every record_* parameter must be a scalar, str-enum, or digest --
    except record_quote_snapshot's `source_counts`, which is a documented,
    deliberate exception: a pre-aggregated {source: count} tally, never a
    per-symbol/raw payload (see quotes.Quotes.status())."""
    allowed_dict_params = {("record_quote_snapshot", "source_counts")}
    for fn in RECORD_METHODS:
        sig = inspect.signature(fn)
        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            ann = param.annotation
            ann_str = str(ann)
            is_dict_annotated = ann_str.startswith("Dict") or ann_str.startswith("dict")
            if is_dict_annotated:
                assert (fn.__name__, pname) in allowed_dict_params, (
                    f"{fn.__name__}({pname}: {ann_str}) accepts a dict payload -- "
                    "record_* methods must take only scalars/digests, or be "
                    "explicitly allowlisted here as a pre-aggregated exception."
                )


def test_record_order_outcome_never_accepts_a_payload_parameter():
    """The order-outcome path is the one most tempting to hand a raw order
    payload to -- pin that it never grows one."""
    params = set(inspect.signature(Metrics.record_order_outcome).parameters)
    assert "payload" not in params
    assert params == {"self", "mode", "status", "broker", "payload_digest"}


# -- cross-process snapshot file (write_snapshot / read_snapshot) -------------
# The _isolated_vesper_state fixture in conftest.py already redirects
# vesper.metrics._DATA_DIR/_SNAPSHOT_PATH to a tmp_path for every test.


def test_read_snapshot_absent_returns_none_without_raising():
    assert read_snapshot() is None


def test_write_then_read_snapshot_round_trips():
    metrics_singleton.record_broker_call("account", "get_account_balance", 0.1, ok=True)
    write_snapshot()
    snap = read_snapshot()
    assert snap is not None
    assert snap["broker_calls"]["account"]["get_account_balance"]["ok"] == 1
    assert "generated_at" in snap


def test_read_snapshot_corrupt_file_returns_none(tmp_path, monkeypatch):
    import vesper.metrics as metrics_module

    bad_path = tmp_path / "corrupt_snapshot.json"
    bad_path.write_text("{not valid json")
    monkeypatch.setattr(metrics_module, "_SNAPSHOT_PATH", bad_path)
    assert metrics_module.read_snapshot() is None


# -- bucket_approval_ages (report-only, no expiry) ----------------------------


def test_bucket_approval_ages_classifies_by_age():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    timestamps = [
        (now - timedelta(minutes=1)).isoformat(),   # under_5min
        (now - timedelta(minutes=10)).isoformat(),  # under_30min
        (now - timedelta(hours=2)).isoformat(),      # stale
    ]
    buckets = bucket_approval_ages(timestamps, now=now)
    assert buckets == {"under_5min": 1, "under_30min": 1, "stale": 1}


def test_bucket_approval_ages_skips_unparseable_timestamps_rather_than_fabricating():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    buckets = bucket_approval_ages(["not-a-timestamp", None, ""], now=now)
    assert buckets == {"under_5min": 0, "under_30min": 0, "stale": 0}


def test_bucket_approval_ages_empty_list():
    assert bucket_approval_ages([]) == {"under_5min": 0, "under_30min": 0, "stale": 0}


def test_bucket_approval_ages_respects_custom_stale_threshold():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    ts = [(now - timedelta(minutes=10)).isoformat()]
    # A 5-minute threshold makes a 10-minute-old approval "stale" instead of
    # "under_30min" -- confirms the threshold is actually read, not hardcoded.
    buckets = bucket_approval_ages(ts, now=now, stale_after_sec=300.0)
    assert buckets == {"under_5min": 0, "under_30min": 0, "stale": 1}


# -- integration: the shapes runner.py/monitor.py actually call through -------


def test_risk_gate_shaped_output_aggregates_like_runner_will():
    """Mirrors vesper/runner.py's risk_gate_node branch: len(proposals) is
    'passed', len(rejected_proposals) is 'rejected'. No import of
    vesper.runner or the LangGraph machinery needed -- this pins the
    aggregation call runner.py makes, not the graph plumbing."""
    m = Metrics()
    fake_output = {"proposals": [object(), object()], "rejected_proposals": [object()]}
    m.record_tool_rejection(
        "risk_gate_node",
        passed=len(fake_output.get("proposals", [])),
        rejected=len(fake_output.get("rejected_proposals", [])),
    )
    snap = m.snapshot()["tool_rejections"]["risk_gate_node"]
    assert snap == {"passed": 2, "rejected": 1}


def test_execution_result_shaped_list_aggregates_like_runner_and_monitor_will():
    """Mirrors both vesper/runner.py's executor_node branch and
    vesper/monitor.py's execute_exit_cascade -- a list of objects with
    .order_proposal_id/.ticker/.status feeds record_order_outcome, never the
    raw ExecutionResult itself."""
    import hashlib

    class _FakeExecutionResult:
        def __init__(self, order_proposal_id, ticker, status):
            self.order_proposal_id = order_proposal_id
            self.ticker = ticker
            self.status = status

    results = [
        _FakeExecutionResult("p1", "AAPL", "SUBMITTED"),
        _FakeExecutionResult("p2", "MSFT", "BLOCKED_BY_GUARDRAIL"),
    ]
    m = Metrics()
    for res in results:
        digest = hashlib.sha256(f"{res.order_proposal_id}:{res.ticker}".encode()).hexdigest()[:16]
        m.record_order_outcome(mode="live", status=res.status, broker="webull", payload_digest=digest)

    snap = m.snapshot()["order_outcomes"]["live"]["webull"]
    assert snap == {"SUBMITTED": 1, "BLOCKED_BY_GUARDRAIL": 1}


# -- wb.py's _retrying() instrumentation (component-level) --------------------
# Same construction pattern as tests/test_wb_credential_logging.py: a fake
# ApiClient/TradeClient/DataClient stands in for the real SDK (never
# installed in CI -- see tests/conftest.py's module docstring).


def _build_webull(monkeypatch):
    import core.wb as wb

    monkeypatch.setattr(wb, "ApiClient", lambda *a, **k: type("C", (), {"add_endpoint": lambda *a, **k: None})())
    monkeypatch.setattr(wb, "TradeClient", lambda c: object())
    monkeypatch.setattr(wb, "DataClient", lambda c: object())
    monkeypatch.setattr(wb, "credentials", lambda: ("key", "secret", "us"))
    monkeypatch.setattr(wb.time, "sleep", lambda *_: None)  # skip the real 2.2s backoff
    return wb.Webull()


def test_retrying_records_ok_broker_call_on_success(monkeypatch):
    from vesper.metrics import metrics as ms

    w = _build_webull(monkeypatch)
    result = w._retrying(lambda: "fine", endpoint="get_account_balance")
    assert result == "fine"
    entry = ms.snapshot()["broker_calls"]["account"]["get_account_balance"]
    assert entry == {"ok": 1, "error": 0, "rate_limited": 0, "p50_ms": entry["p50_ms"], "p95_ms": entry["p95_ms"], "count": 1}


def test_retrying_records_error_broker_call_on_non_429_failure(monkeypatch):
    from vesper.metrics import metrics as ms

    w = _build_webull(monkeypatch)

    def boom():
        raise RuntimeError("500 server error")

    try:
        w._retrying(boom, endpoint="get_account_balance")
    except RuntimeError:
        pass
    entry = ms.snapshot()["broker_calls"]["account"]["get_account_balance"]
    assert entry["ok"] == 0
    assert entry["error"] == 1
    assert entry["rate_limited"] == 0


def test_retrying_records_rate_limited_on_429_then_ok_on_eventual_success(monkeypatch):
    """A 429 that gets retried away must still show up as a rate_limited
    attempt, even though _retrying() itself returns successfully."""
    from vesper.metrics import metrics as ms

    w = _build_webull(monkeypatch)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 TOO_MANY_REQUESTS")
        return "ok-on-retry"

    result = w._retrying(flaky, endpoint="get_account_balance")
    assert result == "ok-on-retry"
    entry = ms.snapshot()["broker_calls"]["account"]["get_account_balance"]
    assert entry["ok"] == 1     # the eventual success
    assert entry["error"] == 1  # the failed first attempt
    assert entry["rate_limited"] == 1


# -- md.py's _cached() instrumentation (component-level) -----------------------


def test_market_cached_records_market_data_broker_call_on_miss_and_hit(monkeypatch):
    import core.md as md
    from vesper.metrics import metrics as ms

    market = md.Market.__new__(md.Market)  # skip __init__'s webull-client wiring
    market._cache = {}
    import threading
    market._lock = threading.Lock()

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return {"ok": True}

    market._cached("snap:US_STOCK:SPY:True", fn, ttl=60.0)
    market._cached("snap:US_STOCK:SPY:True", fn, ttl=60.0)  # cache hit -- fn not called again
    assert calls["n"] == 1

    entry = ms.snapshot()["broker_calls"]["market_data"]["snap"]
    assert entry["ok"] == 1  # only the miss is counted, not the cache hit
    assert entry["error"] == 0


def test_market_cached_records_error_on_raising_fn(monkeypatch):
    import core.md as md
    from vesper.metrics import metrics as ms
    import threading

    market = md.Market.__new__(md.Market)
    market._cache = {}
    market._lock = threading.Lock()

    def fn():
        raise md.MarketDataError("HTTP 403: not entitled")

    try:
        market._cached("scr:vcp", fn, ttl=60.0)
    except md.MarketDataError:
        pass

    entry = ms.snapshot()["broker_calls"]["market_data"]["scr"]
    assert entry["ok"] == 0
    assert entry["error"] == 1
