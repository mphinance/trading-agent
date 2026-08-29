"""Pure, read-only helpers for building approval-card figures from an
OrderProposal -- worst-case notional and a proposal-time digest.

Deliberately NOT inside vesper/execution_guard.py. That module is the only
one that decides whether an order reaches a broker (see its own docstring),
and this task's own ground rules treat it as off-limits to routine edits --
so unlike the original plan for this feature (which would have extracted
`_validate`'s notional math into execution_guard.py itself), the single-leg
strike-vs-premium math below is a small, deliberate DUPLICATE of
execution_guard._validate's branch rather than a shared extraction. It is a
few lines that have not changed since the guard was written, so the drift
risk is low, and keeping it here means this whole module is safe to import
from a read-only card-rendering path -- no ticket store, no halt check, no
VESPER_TRADING gate, nothing stateful.

The multi-leg formulas (THEGA/SYNTHETIC_LONG) are NOT duplicated: they're
read-only-imported from execution_guard._MULTI_LEG_RISK_FORMULAS, so that
math stays defined exactly once.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from vesper.state import OrderProposal


def compute_notional(payload: dict) -> Optional[float]:
    """Single-leg worst-case notional. Mirrors execution_guard._validate's
    strike-vs-premium branch: a short option opened (SELL, not closing)
    commits capital equal to strike*100*qty on assignment, not the premium
    collected. Returns None -- never raises, never fabricates a number --
    when the payload doesn't carry what's needed (e.g. a SELL-to-open
    option missing `strike`, or a non-numeric quantity/price)."""
    try:
        quantity = float(payload.get("quantity") or 0)
    except (TypeError, ValueError):
        return None
    if quantity <= 0:
        return None

    is_option = str(payload.get("asset_type", "")).upper() == "OPTION"
    multiplier = 100.0 if is_option else 1.0
    is_opening_short_option = (
        is_option
        and str(payload.get("side", "")).upper() == "SELL"
        and not payload.get("is_closing", False)
    )
    if is_opening_short_option:
        strike = payload.get("strike")
        if strike is None:
            return None
        try:
            return quantity * float(strike) * multiplier
        except (TypeError, ValueError):
            return None

    try:
        limit_price = float(payload.get("limit_price") or 0)
    except (TypeError, ValueError):
        return None
    return quantity * limit_price * multiplier


def compute_multileg_notional(payload: dict) -> Optional[float]:
    """Multi-leg worst-case notional, reusing execution_guard's own
    registered risk formulas (read-only import -- never touches the
    guard's stateful ticket store or its halt/trading-enabled gates).
    Returns None if strategy_type has no registered formula, legs are
    missing, or the formula itself rejects the leg composition (wrong
    ratio, mismatched strikes, etc.) -- a card-render path must never
    crash on a malformed or in-progress proposal."""
    from vesper.execution_guard import _MULTI_LEG_RISK_FORMULAS, GuardError

    strategy_type = str(payload.get("strategy_type", "")).upper()
    formula = _MULTI_LEG_RISK_FORMULAS.get(strategy_type)
    if formula is None:
        return None
    legs = payload.get("legs") or []
    if not legs:
        return None
    try:
        return formula(legs)
    except GuardError:
        return None


def _single_leg_payload(prop: OrderProposal) -> dict:
    return {
        "symbol": prop.ticker,
        "side": prop.side,
        "quantity": prop.quantity,
        "asset_type": prop.asset_type,
        "limit_price": prop.limit_price,
        "strike": prop.strike,
    }


def _multileg_payload(prop: OrderProposal) -> dict:
    return {
        "symbol": prop.ticker,
        "asset_type": "OPTION",
        "strategy_type": prop.strategy_type,
        "legs": [leg.model_dump() for leg in (prop.legs or [])],
    }


def worst_case_notional(prop: OrderProposal) -> Optional[float]:
    """Worst-case notional for either shape of proposal. None means "could
    not be computed from what's on the proposal" -- the card must omit the
    line rather than show 0 or risk.py's different (premium-based) figure
    (risk_gate.py's own allocation-bucket math uses estimated_cost/premium,
    not this strike-based worst case -- the two are intentionally
    different numbers for different purposes)."""
    if prop.legs:
        return compute_multileg_notional(_multileg_payload(prop))
    return compute_notional(_single_leg_payload(prop))


def proposal_digest(prop: OrderProposal) -> str:
    """A digest of the DRAFTED proposal, not a broker-ready payload: no
    account_id (not resolved until execution) and, for multi-leg, no live
    contract_symbol per leg (also not resolved until execution -- see
    executor.py's multi-leg path). Reuses execution_guard._digest so the
    hash algorithm is defined exactly once, but over a deliberately
    different key set than the execution-time ticket's digest -- see
    ProposalCard.format_text for the caveat line this requires."""
    from vesper.execution_guard import _digest

    shape: Dict[str, Any] = {
        "ticker": prop.ticker,
        "side": prop.side,
        "quantity": prop.quantity,
        "asset_type": prop.asset_type,
        "limit_price": prop.limit_price,
        "strike": prop.strike,
        "expiry": prop.expiry,
        "option_type": prop.option_type,
    }
    if prop.legs:
        shape["strategy_type"] = prop.strategy_type
        shape["legs"] = [leg.model_dump() for leg in prop.legs]
    return _digest(shape)
