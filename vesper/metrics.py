"""Health / observability metrics for Vesper.

A process-wide, thread-safe counter and rolling-duration store for the
"Health/observability metrics" backlog entry in ROADMAP.md: broker latency
+ rate-limit events, LLM call outcomes, risk-gate tool-call rejections,
paper-vs-live order outcomes, and quote-source freshness. Vesper had
effectively none of this before this module.

Report-only, by design. Nothing here gates or blocks execution -- it is
strictly instrumentation, the observability analogue of
core/audit_chain.py's "reports, never refuses" append-only ledger. No
metric here is wired into any risk/gating decision, and none ever will be
from inside this module.

Every `record_*` method takes only scalars, enums (as plain `str`), and
digests the CALLER already hashed (mirroring execution_guard._digest's
`sha256(json.dumps(..., sort_keys=True))` pattern) -- never a raw order
payload, an account id, or a balance. `record_quote_snapshot`'s
`source_counts` is the one dict-shaped parameter, and it is a pre-aggregated
{source: count} tally (see quotes.Quotes.status()), never a per-symbol
payload. See tests/test_metrics.py for the signature-shape check that keeps
this true in code, not just in this docstring.

Thread-safe with one Lock: wb.py's account-bucket calls happen inside
asyncio.to_thread(...) worker threads, so cross-thread writes here are real,
not hypothetical (same reasoning as watcher.py's own `_lock` around
`_fired`).

Every series is bounded in memory -- plain int counters via
collections.Counter, and a `deque(maxlen=...)` of recent durations per
bucket for a rolling p50/p95, matching watcher.py's own `self._fired[-50:]`
bounding discipline. Nothing here grows without limit.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_SNAPSHOT_PATH = _DATA_DIR / "metrics_snapshot.json"

# Bound on the rolling duration window kept per (bucket, endpoint) / per LLM
# tier -- enough for a meaningful p50/p95 without growing without limit.
_DURATIONS_MAXLEN = 200
# Bound on the recent-order-digest trail surfaced in snapshot() -- eyeballing
# aid, not an audit log (that job belongs to core/audit_chain.py).
_RECENT_ORDERS_MAXLEN = 50


def _percentiles(values: "Deque[float]") -> Dict[str, Any]:
    """p50/p95 in milliseconds (plus the sample count), or None-filled for
    an empty series -- never fabricates a percentile from no data."""
    if not values:
        return {"p50_ms": None, "p95_ms": None, "count": 0}
    data = sorted(values)
    p50 = statistics.median(data)
    idx = min(len(data) - 1, max(0, int(round(0.95 * (len(data) - 1)))))
    p95 = data[idx]
    return {"p50_ms": round(p50 * 1000, 1), "p95_ms": round(p95 * 1000, 1), "count": len(data)}


class Metrics:
    """Process-wide aggregator. Construct once -- see the `metrics` singleton
    below, same pattern as `notify.Notifier()`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # record_broker_call
        self._broker_counts: "Counter[Tuple[str, str, str]]" = Counter()  # (bucket, endpoint, "ok"|"error")
        self._broker_rate_limited: "Counter[Tuple[str, str]]" = Counter()  # (bucket, endpoint)
        self._broker_durations: Dict[Tuple[str, str], Deque[float]] = {}

        # record_llm_call
        self._llm_counts: "Counter[Tuple[str, str]]" = Counter()  # (tier, outcome)
        self._llm_durations: Dict[str, Deque[float]] = {}

        # record_tool_rejection
        self._tool_passed: "Counter[str]" = Counter()
        self._tool_rejected: "Counter[str]" = Counter()

        # record_order_outcome
        self._order_counts: "Counter[Tuple[str, str, str]]" = Counter()  # (mode, status, broker)
        self._recent_orders: Deque[Dict[str, Any]] = deque(maxlen=_RECENT_ORDERS_MAXLEN)

        # record_quote_snapshot -- latest reading only. This is a point-in-time
        # gauge (source distribution + staleness), not a time series, so there
        # is nothing to bound here beyond "one entry."
        self._quote_sources: Dict[str, int] = {}
        self._quote_max_age_sec: Optional[float] = None
        self._quote_snapshot_count = 0
        self._quote_recorded_at: Optional[str] = None

    def reset(self) -> None:
        """Testing hook: clear all accumulated state in place. Re-running
        __init__ rather than replacing the module-level `metrics` object
        preserves identity -- every caller does `from vesper.metrics import
        metrics`, so a fresh object here would leave their references
        pointing at stale state instead."""
        self.__init__()

    # -- writers ----------------------------------------------------------

    def record_broker_call(
        self, bucket: str, endpoint: str, duration_sec: float, ok: bool, rate_limited: bool = False,
    ) -> None:
        """bucket: "account" (wb.py's 2 req/2s bucket) or "market_data"
        (md.py's 600/min bucket) -- see wb.py/md.py module docstrings for
        why they're separate."""
        with self._lock:
            self._broker_counts[(bucket, endpoint, "ok" if ok else "error")] += 1
            if rate_limited:
                self._broker_rate_limited[(bucket, endpoint)] += 1
            self._broker_durations.setdefault(
                (bucket, endpoint), deque(maxlen=_DURATIONS_MAXLEN)
            ).append(duration_sec)

    def record_llm_call(self, tier: str, duration_sec: float, outcome: str) -> None:
        """outcome: "ok" | "http_error" | "timeout_or_network" | "json_error"
        | "disabled". "disabled" means is_llm_enabled() was False -- no
        network call was attempted -- distinguishable from an attempt that
        actually failed."""
        with self._lock:
            self._llm_counts[(tier, outcome)] += 1
            self._llm_durations.setdefault(tier, deque(maxlen=_DURATIONS_MAXLEN)).append(duration_sec)

    def record_tool_rejection(self, node: str, passed: int, rejected: int) -> None:
        """The risk-gate tool-call-rejection signal -- node is e.g.
        "risk_gate_node"; passed/rejected are the counts from that node's own
        pass (see vesper/nodes/risk_gate.py's audit_entry, which already
        computes both -- this just aggregates them across passes)."""
        with self._lock:
            self._tool_passed[node] += passed
            self._tool_rejected[node] += rejected

    def record_order_outcome(self, mode: str, status: str, broker: str, payload_digest: str) -> None:
        """The paper-vs-live outcome signal. mode: "paper" | "live". status
        is one of ExecutionResult's own status strings (SUBMITTED,
        DRY_RUN_SIMULATED, BLOCKED_BY_GUARDRAIL, FAILED, REJECTED_BY_USER).
        payload_digest must already be a hash the CALLER produced (mirror
        execution_guard._digest's formula) -- never the payload itself."""
        with self._lock:
            self._order_counts[(mode, status, broker)] += 1
            self._recent_orders.append({
                "mode": mode,
                "status": status,
                "broker": broker,
                "payload_digest": payload_digest,
                "at": datetime.now(timezone.utc).isoformat(),
            })

    def record_quote_snapshot(self, source_counts: Dict[str, int], max_age_sec: Optional[float]) -> None:
        """source_counts is pre-aggregated ({"webull": n, "portfolio": n,
        "tdpro-spot": n}), never a per-symbol payload -- see
        quotes.Quotes.status()."""
        with self._lock:
            self._quote_sources = dict(source_counts)
            self._quote_max_age_sec = max_age_sec
            self._quote_snapshot_count += 1
            self._quote_recorded_at = datetime.now(timezone.utc).isoformat()

    # -- reader -------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Full aggregated view, JSON-serializable. Never raises."""
        with self._lock:
            broker_calls: Dict[str, Dict[str, Any]] = {}
            bucket_endpoint_keys = {k[:2] for k in self._broker_counts} | set(self._broker_durations)
            for bucket, endpoint in bucket_endpoint_keys:
                entry = {
                    "ok": self._broker_counts.get((bucket, endpoint, "ok"), 0),
                    "error": self._broker_counts.get((bucket, endpoint, "error"), 0),
                    "rate_limited": self._broker_rate_limited.get((bucket, endpoint), 0),
                    **_percentiles(self._broker_durations.get((bucket, endpoint), deque())),
                }
                broker_calls.setdefault(bucket, {})[endpoint] = entry

            llm_calls: Dict[str, Dict[str, Any]] = {}
            tiers = {t for (t, _outcome) in self._llm_counts} | set(self._llm_durations)
            for tier in tiers:
                by_outcome = {outcome: n for (t, outcome), n in self._llm_counts.items() if t == tier}
                llm_calls[tier] = {**by_outcome, **_percentiles(self._llm_durations.get(tier, deque()))}

            tool_rejections = {
                node: {
                    "passed": self._tool_passed.get(node, 0),
                    "rejected": self._tool_rejected.get(node, 0),
                }
                for node in set(self._tool_passed) | set(self._tool_rejected)
            }

            order_outcomes: Dict[str, Dict[str, Dict[str, int]]] = {}
            for (mode, status, broker), n in self._order_counts.items():
                order_outcomes.setdefault(mode, {}).setdefault(broker, {})[status] = n

            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "broker_calls": broker_calls,
                "llm_calls": llm_calls,
                "tool_rejections": tool_rejections,
                "order_outcomes": order_outcomes,
                "recent_order_digests": list(self._recent_orders),
                "quote_snapshot": {
                    "sources": dict(self._quote_sources),
                    "max_age_sec": self._quote_max_age_sec,
                    "snapshot_count": self._quote_snapshot_count,
                    "recorded_at": self._quote_recorded_at,
                },
            }


# Process-wide singleton -- same pattern as notify.Notifier() at module level.
metrics = Metrics()


# -- cross-process surfacing --------------------------------------------
#
# `vesper status` (vesper.py) runs as a fresh one-shot process, so it cannot
# see in-process counters accumulated by a separately-running `vesper loop`
# daemon -- the process that actually makes the calls. write_snapshot() is
# called from loop.py's own poll tick (same atomic write pattern halt.py's
# _save_state uses: write a .tmp file, then os.replace) so `status` can read
# a recent-but-not-live view instead of nothing. This is a NEW on-disk state
# file, so tests/conftest.py's _isolated_vesper_state fixture redirects both
# module-level path globals below the same way it does for halt/circuit_
# breaker/paper_ledger/audit_chain/inbound.
#
# Deliberately NOT done: no enforcement tied to any metric here (report-only,
# same as the rest of this module), and no "is this stale" judgment made
# inside read_snapshot() itself -- it hands back generated_at and lets the
# caller (vesper.py's status command) decide how to label it, rather than
# this module inventing a staleness policy nobody asked for.


def write_snapshot() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _SNAPSHOT_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(metrics.snapshot(), f, indent=2)
    os.replace(tmp_path, _SNAPSHOT_PATH)


def read_snapshot() -> Optional[Dict[str, Any]]:
    """The last snapshot written by a running `vesper loop`, or None if one
    was never written (loop never started) or the file can't be read. Never
    raises -- a missing/corrupt snapshot is an ordinary, expected state here,
    not an error worth surfacing as one."""
    if not _SNAPSHOT_PATH.exists():
        return None
    try:
        with open(_SNAPSHOT_PATH) as f:
            return json.load(f)
    except Exception:
        return None


# -- pending-approval age (report-only) ----------------------------------
#
# vesper/bot/inbound.py's ApprovalRegistry.list_pending() already carries
# each item's `registered_at`, but there is no expiry/TTL concept on a
# pending approval anywhere today -- the only thing named "TTL" in this repo
# is execution_guard.Ticket's unrelated, separate 120s post-approval broker
# ticket. This constant is a LABEL for that gap, not a fix for it: it is
# never read by inbound.py (off-limits for this task, and gating logic
# belongs there if it's ever added, not here), and nothing below enforces or
# expires anything -- bucket_approval_ages() only classifies ages for
# display. Whether a "stale" pending approval should ever be auto-rejected
# is a real, separate design decision this module deliberately leaves open.
VESPER_APPROVAL_STALE_AFTER_SEC = float(os.getenv("VESPER_APPROVAL_STALE_AFTER_SEC", "1800"))  # 30 min


def bucket_approval_ages(
    registered_at_iso: List[str],
    now: Optional[datetime] = None,
    stale_after_sec: float = VESPER_APPROVAL_STALE_AFTER_SEC,
) -> Dict[str, int]:
    """Age-bucket a list of ISO-8601 `registered_at` timestamps -- e.g. from
    `[p["registered_at"] for p in approval_registry.list_pending()]` -- into
    {"under_5min", "under_30min", "stale"} counts. Takes only timestamps,
    never a proposal id, session id, or the details dict list_pending()
    items also carry -- same "scalars only" discipline as the rest of this
    module. An unparseable timestamp is skipped, never fabricated into 0."""
    now = now or datetime.now(timezone.utc)
    buckets = {"under_5min": 0, "under_30min": 0, "stale": 0}
    for ts in registered_at_iso:
        try:
            registered = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        if registered.tzinfo is None:
            registered = registered.replace(tzinfo=timezone.utc)
        age = (now - registered).total_seconds()
        if age < 300:
            buckets["under_5min"] += 1
        elif age < stale_after_sec:
            buckets["under_30min"] += 1
        else:
            buckets["stale"] += 1
    return buckets
