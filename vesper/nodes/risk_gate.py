"""Deterministic Risk Gate Node."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.account import FALLBACK_EQUITY, fetch_live_equity
from vesper.state import TradingState, OrderProposal
from vesper.risk import RiskEnforcer

logger = logging.getLogger(__name__)


def _count_open_positions_live() -> tuple[int, float]:
    """(open_long_option_count, wheel_stock_notional) from wb.portfolio()'s
    live positions. Blocking -- wrap in asyncio.to_thread.

    wheel_stock_notional is always 0.0 here: Webull's position data has no
    strategy tagging, so there is no way to tell a wheel-assignment share
    lot apart from any other equity holding without fabricating an
    assumption. See ROADMAP.md for why this is a known, deliberate gap
    rather than an oversight -- closing it needs an assignment-tracking
    mechanism that doesn't exist yet, not a guess bolted onto this function.
    """
    try:
        from wb import Webull

        wb = Webull()
        if not wb.configured:
            return 0, 0.0
        long_option_count = 0
        for account in wb.portfolio().get("accounts", []):
            for pos in account.get("positions", []):
                instrument_type = str(pos.get("instrument_type", "")).upper()
                if "OPTION" in instrument_type and (pos.get("quantity") or 0) > 0:
                    long_option_count += 1
        return long_option_count, 0.0
    except Exception as e:
        logger.warning(f"Could not count live open positions for allocation buckets: {e}")
        return 0, 0.0


def _count_open_positions_paper() -> tuple[int, float]:
    """(open_long_option_count, wheel_stock_notional) from the paper ledger's
    own tracked fills, which DO carry strategy_type -- see paper_ledger.py's
    record_paper_fill. Not blocking (pure JSON file read)."""
    from vesper.paper_ledger import get_paper_positions

    long_option_count = 0
    wheel_stock_notional = 0.0
    for pos in get_paper_positions():
        if pos.get("asset_type") == "OPTION" and str(pos.get("side", "")).upper() in ("BUY", "LONG"):
            long_option_count += 1
        if pos.get("asset_type") == "EQUITY" and pos.get("strategy_type") == "WHEEL_ASSIGNMENT":
            wheel_stock_notional += float(pos.get("total_cost") or 0.0)
    return long_option_count, wheel_stock_notional


async def risk_gate_node(state: TradingState) -> Dict[str, Any]:
    """Applies strict deterministic safety checks before presenting to human."""
    logger.info("-> [RiskGateNode] Enforcing zero-loss budget & risk limits...")

    proposals = state.get("proposals", [])
    valid_proposals: List[OrderProposal] = []
    rejected_proposals: List[OrderProposal] = []
    audit_notes = []
    mode = state.get("mode", "dry_run")

    # One live-equity read per risk-gate pass, not per proposal — wb.py already
    # caches/rate-limits the underlying account-balance call, but there's no
    # reason to hit it more than once here.
    account_equity = await asyncio.to_thread(fetch_live_equity) if proposals else FALLBACK_EQUITY

    # Portfolio-level drawdown circuit breaker -- runs once per pass,
    # independent of whether there are any proposals to check, because a
    # drawdown can trip the breaker even on a scan that drafts nothing.
    # Uses whichever NLV is authoritative for this mode: paper trading
    # shouldn't halt on live-account moves it has nothing to do with, and
    # vice versa.
    if mode == "dry_run":
        from vesper.paper_ledger import get_paper_summary
        current_nlv = get_paper_summary().get("total_nlv", 0.0)
    else:
        current_nlv = account_equity if proposals else await asyncio.to_thread(fetch_live_equity)

    from vesper.circuit_breaker import check_portfolio_drawdown, get_configured_threshold
    breaker_res = check_portfolio_drawdown(current_nlv, threshold_pct=get_configured_threshold())
    if breaker_res.get("tripped_now"):
        audit_notes.append(
            f"🛑 CIRCUIT BREAKER TRIPPED: {breaker_res['drawdown_pct']:.1%} drawdown from peak "
            f"NLV ${breaker_res['peak_nlv']:,.2f} — emergency halt triggered, all proposals blocked."
        )

    # Capital allocation buckets: counted once per pass from whichever
    # position source is authoritative for this mode (see the two helpers
    # above for why they're not interchangeable).
    if mode == "dry_run":
        open_long_option_count, wheel_stock_notional = _count_open_positions_paper()
    else:
        open_long_option_count, wheel_stock_notional = await asyncio.to_thread(_count_open_positions_live)
        audit_notes.append(
            "Capital allocation: wheel-stock bucket not enforced in live mode "
            "(no strategy tagging on live broker positions) — see ROADMAP.md."
        )

    from vesper.llm import audit_proposal_risk, is_llm_enabled
    regime = state.get("regime")
    regime_posture = regime.posture if regime else "NEUTRAL"

    for prop in proposals:
        is_valid, err = RiskEnforcer.validate_proposal(prop, account_equity=account_equity)
        if is_valid:
            is_valid, err = RiskEnforcer.check_capital_allocation_buckets(
                prop,
                open_long_option_count=open_long_option_count,
                wheel_stock_notional=wheel_stock_notional,
                account_equity=account_equity,
            )
            if is_valid and prop.asset_type == "OPTION" and prop.side.upper() in ("BUY", "LONG"):
                # A proposal in THIS batch that will itself become an open
                # long option must count against the next one in the same
                # batch -- otherwise two long-option proposals in one pass
                # could both pass a "max 1" check independently.
                open_long_option_count += 1
        if is_valid:
            # Qualitative LLM Red-Team Audit (if OpenRouter active)
            if is_llm_enabled():
                try:
                    audit_res = await audit_proposal_risk(
                        proposal_dict=prop.model_dump(),
                        regime_posture=regime_posture,
                    )
                    if not audit_res.get("passed", True) or audit_res.get("recommendation") == "REJECT":
                        err = f"LLM Risk Audit Rejection: {', '.join(audit_res.get('concerns', ['Unfavorable risk profile']))}"
                        logger.warning(f"REJECTED by LLM Risk Gate: {prop.id} - {err}")
                        prop.rejection_reason = err
                        rejected_proposals.append(prop)
                        audit_notes.append(f"LLM REJECTED {prop.id}: {err}")
                        continue
                    elif audit_res.get("recommendation") == "REDUCE_SIZE" and prop.quantity > 1:
                        prop.quantity = max(1, prop.quantity // 2)
                        prop.estimated_cost = round(prop.quantity * prop.limit_price, 2)
                        prop.max_risk = round(prop.max_risk * 0.5, 2)
                        audit_notes.append(f"LLM Risk Gate halved position size for {prop.id} (new qty: {prop.quantity})")
                except Exception as e:
                    logger.debug("LLM risk audit skipped: %s", e)

            valid_proposals.append(prop)
            audit_notes.append(f"PASSED Risk Gate: {prop.id} ({prop.ticker} {prop.side} {prop.quantity}x)")
        else:
            logger.warning(f"REJECTED by Risk Gate: {prop.id} - {err}")
            prop.rejection_reason = err
            rejected_proposals.append(prop)
            audit_notes.append(f"REJECTED {prop.id}: {err}")

    audit_entry = {
        "node": "risk_gate_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed_count": len(valid_proposals),
        "rejected_count": len(rejected_proposals),
        "notes": audit_notes,
    }

    return {
        "proposals": valid_proposals,
        "rejected_proposals": rejected_proposals,
        "needs_human_approval": len(valid_proposals) > 0,
        "audit_trail": [audit_entry],
    }
