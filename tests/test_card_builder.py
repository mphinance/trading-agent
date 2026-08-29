"""Tests for vesper/bot/card_builder.py -- the pure, read-only worst-case
notional and proposal-digest helpers used by ProposalCard.from_proposal.

Deliberately separate from tests/test_execution_guard.py: card_builder.py
duplicates (rather than shares) execution_guard._validate's single-leg
notional math because execution_guard.py is off-limits to routine edits --
see card_builder.py's own docstring. These tests pin THIS module's
behavior, not the guard's.
"""

from __future__ import annotations

from vesper.bot.card_builder import (
    compute_notional,
    compute_multileg_notional,
    worst_case_notional,
    proposal_digest,
)
from vesper.state import OrderProposal, OrderLeg


# ── compute_notional (single-leg) ────────────────────────────────────────

def test_compute_notional_equity_buy_is_price_times_quantity():
    payload = {"symbol": "AAPL", "side": "BUY", "quantity": 10, "asset_type": "EQUITY", "limit_price": 200.0}
    assert compute_notional(payload) == 2000.0


def test_compute_notional_long_option_uses_premium_not_strike():
    payload = {
        "symbol": "AAPL", "side": "BUY", "quantity": 1, "asset_type": "OPTION",
        "limit_price": 8.50, "strike": 190.0,
    }
    assert compute_notional(payload) == 850.0  # 1 * 8.50 * 100, not strike-based


def test_compute_notional_short_option_open_uses_strike():
    payload = {
        "symbol": "AAPL", "side": "SELL", "quantity": 1, "asset_type": "OPTION",
        "limit_price": 2.50, "strike": 190.0,
    }
    assert compute_notional(payload) == 19000.0  # 1 * 190 * 100


def test_compute_notional_short_option_open_missing_strike_returns_none():
    """A SELL-to-open option with no strike can't be risk-assessed from the
    premium alone -- must return None (never raise, never fabricate 0)."""
    payload = {"symbol": "AAPL", "side": "SELL", "quantity": 1, "asset_type": "OPTION", "limit_price": 2.50}
    assert compute_notional(payload) is None


def test_compute_notional_closing_sell_uses_premium():
    """A SELL that's CLOSING an existing long (is_closing=True) is priced
    off the market value, not the strike -- same branch executor.py's
    payload["is_closing"] flag drives in execution_guard."""
    payload = {
        "symbol": "AAPL", "side": "SELL", "quantity": 1, "asset_type": "OPTION",
        "limit_price": 4.00, "strike": 190.0, "is_closing": True,
    }
    assert compute_notional(payload) == 400.0  # 1 * 4.00 * 100


def test_compute_notional_zero_quantity_returns_none():
    payload = {"symbol": "AAPL", "side": "BUY", "quantity": 0, "asset_type": "EQUITY", "limit_price": 200.0}
    assert compute_notional(payload) is None


# ── compute_multileg_notional ────────────────────────────────────────────

def test_compute_multileg_notional_unregistered_strategy_returns_none():
    payload = {"symbol": "AAPL", "strategy_type": "IRON_CONDOR", "legs": [{"quantity": 1}]}
    assert compute_multileg_notional(payload) is None


def test_compute_multileg_notional_no_legs_returns_none():
    payload = {"symbol": "AAPL", "strategy_type": "SYNTHETIC_LONG", "legs": []}
    assert compute_multileg_notional(payload) is None


def test_compute_multileg_notional_malformed_composition_returns_none_not_raise():
    """SYNTHETIC_LONG requires exactly 2 legs -- a card-render path must
    never crash on a malformed proposal, unlike execution_guard's own
    _validate_multileg which raises for the same input."""
    payload = {
        "symbol": "AAPL", "strategy_type": "SYNTHETIC_LONG",
        "legs": [{"side": "BUY", "option_type": "call", "strike": 100.0, "expiry": "2026-09-18", "quantity": 1}],
    }
    assert compute_multileg_notional(payload) is None


def test_compute_multileg_notional_synthetic_long_matches_strike_formula():
    payload = {
        "symbol": "AAPL", "strategy_type": "SYNTHETIC_LONG",
        "legs": [
            {"side": "BUY", "option_type": "call", "strike": 100.0, "expiry": "2026-09-18", "quantity": 2},
            {"side": "SELL", "option_type": "put", "strike": 100.0, "expiry": "2026-09-18", "quantity": 2},
        ],
    }
    assert compute_multileg_notional(payload) == 20000.0  # 100 * 100 * 2


# ── worst_case_notional (dispatches on prop.legs) ────────────────────────

def test_worst_case_notional_single_leg_proposal():
    prop = OrderProposal(
        id="p1", ticker="NVDA", asset_type="OPTION", side="SELL",
        limit_price=2.0, quantity=1, strike=120.0,
    )
    assert worst_case_notional(prop) == 12000.0


def test_worst_case_notional_multileg_proposal():
    prop = OrderProposal(
        id="p2", ticker="AAPL", asset_type="OPTION", side="BUY", limit_price=0.0, quantity=1,
        strategy_type="SYNTHETIC_LONG",
        legs=[
            OrderLeg(side="BUY", option_type="call", strike=100.0, expiry="2026-09-18", quantity=1, limit_price=5.0),
            OrderLeg(side="SELL", option_type="put", strike=100.0, expiry="2026-09-18", quantity=1, limit_price=4.0),
        ],
    )
    assert worst_case_notional(prop) == 10000.0


def test_worst_case_notional_none_when_uncomputable():
    prop = OrderProposal(
        id="p3", ticker="AAPL", asset_type="OPTION", side="SELL",
        limit_price=2.0, quantity=1, strike=None,
    )
    assert worst_case_notional(prop) is None


# ── proposal_digest ───────────────────────────────────────────────────────

def test_proposal_digest_is_deterministic():
    prop = OrderProposal(id="p4", ticker="NVDA", asset_type="EQUITY", side="BUY", limit_price=100.0, quantity=5)
    assert proposal_digest(prop) == proposal_digest(prop)


def test_proposal_digest_differs_from_executor_time_payload_digest():
    """Card claims this is a PROPOSAL digest, not a ticket digest -- must be
    provably a different hash than the broker-ready payload executor.py
    builds (which carries account_id and no proposal `ticker` field),
    verifying the digest's own docstring claim rather than just trusting it."""
    from vesper.execution_guard import _digest

    prop = OrderProposal(id="p5", ticker="NVDA", asset_type="EQUITY", side="BUY", limit_price=100.0, quantity=5)
    executor_payload = {
        "account_id": "acct-123",
        "symbol": prop.ticker,
        "side": prop.side,
        "order_type": prop.order_type,
        "limit_price": prop.limit_price,
        "quantity": prop.quantity,
        "asset_type": prop.asset_type,
        "time_in_force": "DAY",
        "strike": prop.strike,
    }
    assert proposal_digest(prop) != _digest(executor_payload)


def test_proposal_digest_multileg_includes_legs():
    prop_a = OrderProposal(
        id="p6", ticker="AAPL", asset_type="OPTION", side="BUY", limit_price=0.0, quantity=1,
        strategy_type="SYNTHETIC_LONG",
        legs=[OrderLeg(side="BUY", option_type="call", strike=100.0, expiry="2026-09-18", quantity=1, limit_price=5.0)],
    )
    prop_b = OrderProposal(
        id="p6", ticker="AAPL", asset_type="OPTION", side="BUY", limit_price=0.0, quantity=1,
        strategy_type="SYNTHETIC_LONG",
        legs=[OrderLeg(side="BUY", option_type="call", strike=105.0, expiry="2026-09-18", quantity=1, limit_price=5.0)],
    )
    assert proposal_digest(prop_a) != proposal_digest(prop_b)
