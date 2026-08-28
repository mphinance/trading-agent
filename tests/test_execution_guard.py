"""Pins vesper.execution_guard's safety properties the way the deleted
tests/test_orders.py pinned orders.py's — see docs/CODE_SWEEP_2026-08-28.md.
"""

from __future__ import annotations

import time

import pytest

from vesper.execution_guard import ExecutionGuard, GuardError, TradingDisabled

BASE_PAYLOAD = {
    "symbol": "AAPL",
    "side": "BUY",
    "quantity": 1,
    "limit_price": 100.0,
    "asset_type": "EQUITY",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "VESPER_TRADING",
        "VESPER_MAX_NOTIONAL",
        "VESPER_MAX_QUANTITY",
        "VESPER_MAX_BP_FRACTION",
        "VESPER_SYMBOL_ALLOWLIST",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def guard():
    return ExecutionGuard()


def test_kill_switch_defaults_off(guard):
    with pytest.raises(TradingDisabled):
        guard.preview("p1", dict(BASE_PAYLOAD))


def test_kill_switch_allows_when_enabled(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    ticket = guard.preview("p1", dict(BASE_PAYLOAD))
    assert ticket.proposal_id == "p1"


def test_notional_cap_rejects_oversized_order(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "50")
    with pytest.raises(GuardError, match="notional"):
        guard.preview("p1", dict(BASE_PAYLOAD))  # 1 * $100 > $50 cap


def test_quantity_cap_rejects_oversized_order(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_QUANTITY", "1")
    payload = {**BASE_PAYLOAD, "quantity": 5}
    with pytest.raises(GuardError, match="quantity"):
        guard.preview("p1", payload)


def test_symbol_allowlist_blocks_unlisted_symbol(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_SYMBOL_ALLOWLIST", "SPY,QQQ")
    with pytest.raises(GuardError, match="(?i)allowlist"):
        guard.preview("p1", dict(BASE_PAYLOAD))  # AAPL not in SPY,QQQ


def test_symbol_allowlist_allows_listed_symbol(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_SYMBOL_ALLOWLIST", "AAPL,QQQ")
    guard.preview("p1", dict(BASE_PAYLOAD))  # should not raise


def test_ticket_is_single_use(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    payload = dict(BASE_PAYLOAD)
    ticket = guard.preview("p1", payload)
    guard.place(ticket.id, payload, lambda: {"status": "ok"})
    with pytest.raises(GuardError, match="already used"):
        guard.place(ticket.id, payload, lambda: {"status": "ok"})


def test_ticket_expires(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    payload = dict(BASE_PAYLOAD)
    ticket = guard.preview("p1", payload)
    ticket.created_at = time.time() - 1000  # force past the TTL
    with pytest.raises(GuardError, match="expired"):
        guard.place(ticket.id, payload, lambda: {"status": "ok"})


def test_place_refuses_mismatched_payload(monkeypatch, guard):
    """The core guarantee: what gets placed must be byte-for-byte what was
    previewed. A caller building a different payload after preview must be
    rejected, not silently placed."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    previewed = dict(BASE_PAYLOAD)
    ticket = guard.preview("p1", previewed)

    tampered = {**BASE_PAYLOAD, "quantity": 1000}
    with pytest.raises(GuardError, match="does not match"):
        guard.place(ticket.id, tampered, lambda: {"status": "ok"})


def test_place_calls_broker_fn_exactly_once_on_success(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    payload = dict(BASE_PAYLOAD)
    ticket = guard.preview("p1", payload)

    calls = []

    def _place_fn():
        calls.append(1)
        return {"status": "filled"}

    result = guard.place(ticket.id, payload, _place_fn)
    assert result == {"status": "filled"}
    assert len(calls) == 1


def test_unknown_ticket_id_rejected(guard):
    with pytest.raises(GuardError, match="unknown"):
        guard.place("not-a-real-ticket", dict(BASE_PAYLOAD), lambda: {"status": "ok"})
