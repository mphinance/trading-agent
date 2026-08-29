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


# ── SELL-to-open option notional: strike, not premium ──────────────────────
# A short option's real capital commitment is the strike, not the few
# dollars of premium in limit_price. Regression tests for the bug where a
# cash-secured put could sail past VESPER_MAX_NOTIONAL because the guard was
# reading the premium instead of the strike.

def test_sell_to_open_option_requires_strike(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    payload = {
        "symbol": "AAPL", "side": "SELL", "quantity": 1,
        "limit_price": 2.50, "asset_type": "OPTION",
    }
    with pytest.raises(GuardError, match="strike"):
        guard.preview("p1", payload)


def test_sell_to_open_option_notional_uses_strike_not_premium(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "5000")
    # $2.50 premium * 100 = $250 (would pass a $5000 cap easily), but the
    # real commitment is a $190 strike * 100 = $19,000 -- must be rejected.
    payload = {
        "symbol": "AAPL", "side": "SELL", "quantity": 1,
        "limit_price": 2.50, "asset_type": "OPTION", "strike": 190.0,
    }
    with pytest.raises(GuardError, match="notional"):
        guard.preview("p1", payload)


def test_sell_to_open_option_within_strike_based_cap_passes(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "25000")
    payload = {
        "symbol": "AAPL", "side": "SELL", "quantity": 1,
        "limit_price": 2.50, "asset_type": "OPTION", "strike": 190.0,
    }
    guard.preview("p1", payload)  # should not raise


def test_sell_to_close_option_uses_premium_not_strike(monkeypatch, guard):
    """monitor.py's exit cascade closes an existing long option position --
    limit_price*100*qty (the market value) is correct here, and no strike
    should be required (a paper/legacy fill may not carry one)."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "5000")
    payload = {
        "symbol": "AAPL", "side": "SELL", "quantity": 1,
        "limit_price": 2.50, "asset_type": "OPTION", "is_closing": True,
    }
    guard.preview("p1", payload)  # should not raise -- no strike needed to close


# ── Multi-leg (combo) orders: SYNTHETIC_LONG ────────────────────────────────
# Only a small whitelist of strategy_type values has a registered risk
# formula (execution_guard._MULTI_LEG_RISK_FORMULAS). Anything else must be
# refused outright rather than guessed at -- the same "refuse rather than
# under-count" principle as the single-leg strike-vs-premium fix above.

def _synth_legs(strike=190.0, expiry="2025-06-20", call_premium=8.0, put_premium=6.0, qty=1):
    return [
        {"side": "BUY", "option_type": "call", "strike": strike, "expiry": expiry,
         "quantity": qty, "limit_price": call_premium},
        {"side": "SELL", "option_type": "put", "strike": strike, "expiry": expiry,
         "quantity": qty, "limit_price": put_premium},
    ]


def test_synthetic_long_notional_uses_strike_like_short_put(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "25000")
    payload = {
        "symbol": "AAPL", "asset_type": "OPTION", "strategy_type": "SYNTHETIC_LONG",
        "legs": _synth_legs(strike=190.0),
    }
    guard.preview("p1", payload)  # $190 * 100 * 1 = $19,000 -- within cap, should not raise


def test_synthetic_long_exceeds_notional_cap_rejected(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "5000")
    payload = {
        "symbol": "AAPL", "asset_type": "OPTION", "strategy_type": "SYNTHETIC_LONG",
        "legs": _synth_legs(strike=190.0),
    }
    with pytest.raises(GuardError, match="max risk"):
        guard.preview("p1", payload)


def test_unregistered_multileg_strategy_type_refused(monkeypatch, guard):
    """A strategy_type with no registered risk formula must be refused, not
    silently approximated."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    payload = {
        "symbol": "AAPL", "asset_type": "OPTION", "strategy_type": "UNKNOWN_STRATEGY",
        "legs": _synth_legs(),
    }
    with pytest.raises(GuardError, match="no registered risk formula"):
        guard.preview("p1", payload)


def test_synthetic_long_wrong_leg_count_rejected(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    payload = {
        "symbol": "AAPL", "asset_type": "OPTION", "strategy_type": "SYNTHETIC_LONG",
        "legs": _synth_legs()[:1],
    }
    with pytest.raises(GuardError, match="exactly 2 legs"):
        guard.preview("p1", payload)


def test_synthetic_long_mismatched_strike_rejected(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    legs = _synth_legs(strike=190.0)
    legs[1]["strike"] = 195.0  # put leg strike drifted from call leg
    payload = {
        "symbol": "AAPL", "asset_type": "OPTION", "strategy_type": "SYNTHETIC_LONG",
        "legs": legs,
    }
    with pytest.raises(GuardError, match="same strike"):
        guard.preview("p1", payload)


def test_synthetic_long_mismatched_expiry_rejected(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    legs = _synth_legs()
    legs[1]["expiry"] = "2025-09-19"  # put leg expiry drifted from call leg
    payload = {
        "symbol": "AAPL", "asset_type": "OPTION", "strategy_type": "SYNTHETIC_LONG",
        "legs": legs,
    }
    with pytest.raises(GuardError, match="same expiry"):
        guard.preview("p1", payload)


def test_synthetic_long_wrong_sides_rejected(monkeypatch, guard):
    """Buying the put and selling the call is not a synthetic long -- it's the
    inverse position (synthetic short) and must not slip through with the
    long's risk formula."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    legs = _synth_legs()
    legs[0]["side"], legs[1]["side"] = "SELL", "BUY"
    payload = {
        "symbol": "AAPL", "asset_type": "OPTION", "strategy_type": "SYNTHETIC_LONG",
        "legs": legs,
    }
    with pytest.raises(GuardError, match="CALL leg to be BUY"):
        guard.preview("p1", payload)


def test_synthetic_long_kill_switch_still_applies(guard):
    payload = {
        "symbol": "AAPL", "asset_type": "OPTION", "strategy_type": "SYNTHETIC_LONG",
        "legs": _synth_legs(),
    }
    with pytest.raises(TradingDisabled):
        guard.preview("p1", payload)


def test_synthetic_long_ticket_place_round_trip(monkeypatch, guard):
    """The ticket handshake (preview -> place, digest match, single-use)
    works identically for a multi-leg payload as for a single-leg one."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "25000")
    payload = {
        "symbol": "AAPL", "asset_type": "OPTION", "strategy_type": "SYNTHETIC_LONG",
        "legs": _synth_legs(),
    }
    ticket = guard.preview("p1", payload)
    result = guard.place(ticket.id, payload, lambda: {"status": "filled"})
    assert result == {"status": "filled"}
    with pytest.raises(GuardError, match="already used"):
        guard.place(ticket.id, payload, lambda: {"status": "filled"})


# ── Multi-leg (combo) orders: THEGA ─────────────────────────────────────────
# 100 shares + 1 ATM covered call + 3 ATM CSPs, same strike/expiry. Worst-case
# risk is the strike-based CSP assignment notional plus the equity notional
# (shares as a total loss) -- never the premium collected.

def _thega_legs(strike=50.0, expiry="2025-06-20", equity_price=50.0,
                 call_premium=1.5, put_premium=1.2,
                 equity_qty=100, call_qty=1, put_qty=3):
    return [
        {"side": "BUY", "asset_type": "EQUITY", "quantity": equity_qty, "limit_price": equity_price},
        {"side": "SELL", "asset_type": "OPTION", "option_type": "call", "strike": strike,
         "expiry": expiry, "quantity": call_qty, "limit_price": call_premium},
        {"side": "SELL", "asset_type": "OPTION", "option_type": "put", "strike": strike,
         "expiry": expiry, "quantity": put_qty, "limit_price": put_premium},
    ]


def test_thega_notional_is_equity_plus_put_assignment(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "25000")
    monkeypatch.setenv("VESPER_MAX_QUANTITY", "100")
    payload = {
        "symbol": "AAPL", "asset_type": "EQUITY", "strategy_type": "THEGA",
        "legs": _thega_legs(strike=50.0, equity_price=50.0),
    }
    # equity: 100*50=$5,000 + put assignment: 50*100*3=$15,000 = $20,000
    guard.preview("p1", payload)  # should not raise -- within $25,000 cap


def test_thega_exceeds_notional_cap_rejected(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "10000")
    monkeypatch.setenv("VESPER_MAX_QUANTITY", "100")
    payload = {
        "symbol": "AAPL", "asset_type": "EQUITY", "strategy_type": "THEGA",
        "legs": _thega_legs(strike=50.0, equity_price=50.0),
    }
    with pytest.raises(GuardError, match="max risk"):
        guard.preview("p1", payload)


def test_thega_wrong_leg_count_rejected(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_QUANTITY", "100")
    payload = {
        "symbol": "AAPL", "asset_type": "EQUITY", "strategy_type": "THEGA",
        "legs": _thega_legs()[:2],
    }
    with pytest.raises(GuardError, match="exactly 3 legs"):
        guard.preview("p1", payload)


def test_thega_wrong_ratio_rejected(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_QUANTITY", "1000")
    payload = {
        "symbol": "AAPL", "asset_type": "EQUITY", "strategy_type": "THEGA",
        "legs": _thega_legs(equity_qty=200, put_qty=3),  # 200 shares, not 100
    }
    with pytest.raises(GuardError, match="fixed-ratio strategy"):
        guard.preview("p1", payload)


def test_thega_mismatched_strike_rejected(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_QUANTITY", "100")
    legs = _thega_legs(strike=50.0)
    legs[2]["strike"] = 55.0  # put strike drifted from call strike
    payload = {"symbol": "AAPL", "asset_type": "EQUITY", "strategy_type": "THEGA", "legs": legs}
    with pytest.raises(GuardError, match="same .*ATM.* strike"):
        guard.preview("p1", payload)


def test_thega_wrong_sides_rejected(monkeypatch, guard):
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_QUANTITY", "100")
    legs = _thega_legs()
    legs[0]["side"] = "SELL"  # shorting the shares instead of owning them
    payload = {"symbol": "AAPL", "asset_type": "EQUITY", "strategy_type": "THEGA", "legs": legs}
    with pytest.raises(GuardError, match="EQUITY leg to be BUY"):
        guard.preview("p1", payload)


def test_thega_missing_leg_type_rejected(monkeypatch, guard):
    """Two option legs and no equity leg (or vice versa) must be refused, not
    silently priced as if the missing leg didn't matter."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_QUANTITY", "100")
    legs = _thega_legs()
    legs[0]["asset_type"] = "OPTION"
    legs[0]["option_type"] = "put"
    legs[0]["strike"] = 50.0
    legs[0]["expiry"] = "2025-06-20"
    payload = {"symbol": "AAPL", "asset_type": "EQUITY", "strategy_type": "THEGA", "legs": legs}
    with pytest.raises(GuardError, match="one EQUITY leg"):
        guard.preview("p1", payload)
