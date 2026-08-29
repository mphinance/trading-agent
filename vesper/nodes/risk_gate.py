"""Deterministic Risk Gate Node."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.account import FALLBACK_EQUITY, fetch_live_equity, fetch_live_buying_power
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


def _sector_notional_paper() -> Dict[str, float]:
    """Sector -> total open notional, from the paper ledger's own tracked
    fills. `ticker` on a paper fill is already the clean underlying
    (OrderProposal.ticker is separate from contract_symbol/strike/expiry/
    option_type -- see vesper/state.py -- and record_paper_fill persists
    that same `ticker` field), so no OCC/contract-symbol parsing is needed
    here, unlike the live-mode OPTION gap below."""
    from vesper.paper_ledger import get_paper_positions
    from vesper.sector import get_sector

    out: Dict[str, float] = {}
    for pos in get_paper_positions():
        sector = get_sector(pos.get("ticker", ""))
        if sector is None:
            logger.info(f"Sector concentration: skipping {pos.get('ticker')} (sector unresolved)")
            continue
        out[sector] = out.get(sector, 0.0) + float(pos.get("total_cost") or 0.0)
    return out


def _sector_notional_live() -> Dict[str, float]:
    """Sector -> total open notional, from wb.portfolio()'s live positions.
    Blocking -- wrap in asyncio.to_thread.

    EQUITY positions are counted in full: wb.py's `symbol` field IS the
    ticker for equities, so vesper.sector.get_sector resolves it directly.

    OPTION positions are skipped, with an audit note (see risk_gate_node):
    wb.py's _position() normalizer has no underlying-ticker field distinct
    from the option contract symbol (confirmed by reading _position() and
    grepping wb.py + docs/webull-api/ for "underlying" -- zero hits), so
    there is no way to resolve an OPTION position's sector without guessing
    at the underlying from the contract symbol. That would be exactly the
    kind of fabrication rule 1 forbids, so this skips rather than parses an
    assumed OCC root symbol. Same honesty pattern as the existing
    wheel-stock live-mode gap in _count_open_positions_live above.
    """
    from wb import Webull
    from vesper.sector import get_sector

    wb = Webull()
    if not wb.configured:
        return {}
    out: Dict[str, float] = {}
    for account in wb.portfolio().get("accounts", []):
        for pos in account.get("positions", []):
            if "OPTION" in str(pos.get("instrument_type", "")).upper():
                logger.info(
                    f"Sector concentration: OPTION position {pos.get('symbol')} skipped in "
                    "live mode (underlying ticker not exposed by wb.py yet)"
                )
                continue
            sector = get_sector(pos.get("symbol", ""))
            if sector is None:
                logger.info(f"Sector concentration: skipping {pos.get('symbol')} (sector unresolved)")
                continue
            out[sector] = out.get(sector, 0.0) + float(pos.get("market_value") or 0.0)
    return out


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
    # Per-proposal allocation-bucket snapshot, captured mid-loop (see below)
    # BEFORE each proposal's own same-batch-stacking increment -- carrying
    # only the final post-loop totals forward would show every proposal in
    # a multi-proposal batch the same post-batch numbers, not the state as
    # of when it was actually evaluated. Keyed by proposal.id, surfaced to
    # human_gate_node's approval card via the `capital_snapshot` return key.
    capital_snapshots: Dict[str, dict] = {}

    # One live-equity read per risk-gate pass, not per proposal — wb.py already
    # caches/rate-limits the underlying account-balance call, but there's no
    # reason to hit it more than once here.
    account_equity = await asyncio.to_thread(fetch_live_equity) if proposals else FALLBACK_EQUITY

    # Live buying power, for the approval card's buying-power-impact line
    # (see vesper/bot/base.py ProposalCard). Unlike account_equity there is
    # no accepted fallback constant -- None means "unknown", and the card
    # must omit the line rather than guess.
    live_buying_power = await asyncio.to_thread(fetch_live_buying_power) if proposals else None
    if proposals and live_buying_power is None and mode != "dry_run":
        audit_notes.append(
            "Capital allocation: live buying power unavailable this pass — "
            "approval card will omit buying-power-impact for these proposals."
        )

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
        sector_notional = _sector_notional_paper()
    else:
        open_long_option_count, wheel_stock_notional = await asyncio.to_thread(_count_open_positions_live)
        sector_notional = await asyncio.to_thread(_sector_notional_live)
        audit_notes.append(
            "Capital allocation: wheel-stock bucket not enforced in live mode "
            "(no strategy tagging on live broker positions) — see ROADMAP.md."
        )
        audit_notes.append(
            "Capital allocation: sector-concentration bucket does not count live OPTION "
            "positions (no underlying-ticker field exposed by wb.py yet) — see ROADMAP.md."
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
            if is_valid:
                # Snapshot BEFORE the same-batch-stacking increment just
                # below, so a before/after diff on the approval card
                # reflects state as of when THIS proposal was evaluated,
                # not the batch's final post-loop totals.
                capital_snapshots[prop.id] = {
                    "open_long_option_count_before": open_long_option_count,
                    "wheel_stock_notional_before": wheel_stock_notional,
                    "sector": None,
                    "sector_notional_before": None,
                }
            if is_valid and prop.asset_type == "OPTION" and prop.side.upper() in ("BUY", "LONG"):
                # A proposal in THIS batch that will itself become an open
                # long option must count against the next one in the same
                # batch -- otherwise two long-option proposals in one pass
                # could both pass a "max 1" check independently.
                open_long_option_count += 1
        if is_valid:
            # Only a proposal that ADDS exposure runs the sector-concentration
            # bucket (mirrors check_sector_concentration's own is_closing/side
            # gate) -- skip the sector lookup entirely for a SELL/closing
            # proposal rather than paying for a network call whose result
            # would be discarded.
            is_adding_exposure = prop.side.upper() in ("BUY", "LONG") and not getattr(prop, "is_closing", False)
            sector = None
            if is_adding_exposure:
                from vesper.sector import get_sector
                sector = await asyncio.to_thread(get_sector, prop.ticker)
            if prop.id in capital_snapshots:
                # A SELL/closing proposal never reaches here with a sector
                # (is_adding_exposure gates the lookup above) -- snapshot
                # stays None rather than fabricating one, matching the same
                # honesty rule the live-mode OPTION-sector gap already
                # follows.
                capital_snapshots[prop.id]["sector"] = sector
                capital_snapshots[prop.id]["sector_notional_before"] = (
                    sector_notional.get(sector, 0.0) if sector else None
                )
            is_valid, err = RiskEnforcer.check_sector_concentration(
                prop,
                sector=sector,
                sector_notional=sector_notional,
                account_equity=account_equity,
            )
            if is_valid and sector and is_adding_exposure:
                # Same same-batch-stacking rationale as open_long_option_count
                # above: a proposal in this batch that will itself add sector
                # notional must count against the next one in the same batch.
                added = prop.estimated_cost or (
                    prop.limit_price * prop.quantity * (100 if prop.asset_type == "OPTION" else 1)
                )
                sector_notional[sector] = sector_notional.get(sector, 0.0) + added
        if is_valid:
            # Qualitative LLM Red-Team Audit (if OpenRouter active)
            if is_llm_enabled():
                try:
                    audit_res = await audit_proposal_risk(
                        proposal_dict=prop.model_dump(),
                        regime_posture=regime_posture,
                    )
                    # Normalise before comparing. audit_proposal_risk returns the
                    # model's parsed JSON as-is -- llm.py only uppercases into a
                    # local for its own escalation decision, so the value that
                    # arrives here is whatever the model actually wrote. Comparing
                    # it case-sensitively meant a model answering "reject" instead
                    # of "REJECT" matched NOTHING and fell straight through to
                    # valid_proposals at full size: a fail-OPEN in the risk gate,
                    # triggered by nothing more than model wording drift.
                    recommendation = str(audit_res.get("recommendation", "")).strip().upper()

                    if not audit_res.get("passed", True) or recommendation == "REJECT":
                        err = f"LLM Risk Audit Rejection: {', '.join(audit_res.get('concerns', ['Unfavorable risk profile']))}"
                        logger.warning(f"REJECTED by LLM Risk Gate: {prop.id} - {err}")
                        prop.rejection_reason = err
                        rejected_proposals.append(prop)
                        audit_notes.append(f"LLM REJECTED {prop.id}: {err}")
                        continue
                    elif recommendation == "REDUCE_SIZE" and prop.quantity > 1:
                        if prop.legs:
                            # A multi-leg combo's quantity is not independently
                            # scalable: THEGA is a fixed 100:1:3 ratio and
                            # SYNTHETIC_LONG a fixed 1:1, both enforced by
                            # execution_guard's per-strategy formula. Halving the
                            # top-level quantity without scaling the legs produces
                            # a proposal the guard will refuse outright. It would
                            # fail closed, but pointlessly -- so decline to resize
                            # and say so, leaving the proposal intact for the human.
                            audit_notes.append(
                                f"LLM Risk Gate suggested REDUCE_SIZE for {prop.id} but it is a "
                                f"multi-leg {prop.strategy_type} with a fixed leg ratio — left unchanged"
                            )
                        else:
                            old_qty = prop.quantity
                            prop.quantity = max(1, prop.quantity // 2)
                            # Options are priced per share and trade in 100-share
                            # contracts. Recomputing without the multiplier (as this
                            # did) understated an option's capital-at-risk by 100x
                            # on the human approval card -- the one number a
                            # reviewer most needs to be right.
                            multiplier = 100 if prop.asset_type == "OPTION" else 1
                            prop.estimated_cost = round(prop.quantity * prop.limit_price * multiplier, 2)
                            # Scale max_risk by the ACTUAL quantity change, not a
                            # flat 0.5 -- integer division means 3 -> 1 is a third,
                            # not a half.
                            prop.max_risk = round(prop.max_risk * (prop.quantity / old_qty), 2)
                            audit_notes.append(
                                f"LLM Risk Gate reduced {prop.id} from {old_qty} to {prop.quantity} "
                                f"(est cost ${prop.estimated_cost:,.2f}, max risk ${prop.max_risk:,.2f})"
                            )
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
        "account_equity": account_equity,
        "live_buying_power": live_buying_power,
        "capital_snapshot": capital_snapshots,
    }
