"""Post-Trade Reflection & Thesis Journaling Node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.state import TradingState
from core.conviction import log_conviction, resolve_convictions

logger = logging.getLogger(__name__)


async def reflection_node(state: TradingState) -> Dict[str, Any]:
    """Records trade thesis, conviction score, and post-session reflection across all candidate tiers."""
    logger.info("-> [ReflectionNode] Logging conviction & journaling session outcomes...")

    execution_results = state.get("execution_results", [])
    proposals = state.get("proposals", [])
    rejected_proposals = state.get("rejected_proposals", [])
    candidates = state.get("candidates", [])
    technicals = state.get("technicals", {})
    regime = state.get("regime")
    session_id = state.get("session_id")
    playbook = state.get("selected_playbook")
    human_decision = state.get("human_decision")
    reflection_notes: List[str] = []

    logged_prop_ids = set()
    logged_tickers = set()
    prop_map = {p.id: p for p in proposals + rejected_proposals}

    # ── 1. Log Executed / Staged Results ─────────────────────────────────────
    for res in execution_results:
        ticker = res.ticker
        tech = technicals.get(ticker)
        prop = prop_map.get(res.order_proposal_id)
        logged_prop_ids.add(res.order_proposal_id)
        logged_tickers.add(ticker)

        # Derive direction accurately from proposal side or fallback
        if prop and prop.side:
            direction = "bullish" if prop.side.upper() == "BUY" else "bearish"
        elif "BUY" in str(res.message).upper():
            direction = "bullish"
        elif "SELL" in str(res.message).upper():
            direction = "bearish"
        else:
            direction = "bullish"

        # Determine origin lifecycle stage
        if "REJECTED_BY_USER" in res.status:
            origin = "REJECTED_BY_USER"
            not_taken_reason = res.message or "Rejected by user"
        elif "BLOCKED" in res.status or "GUARD" in res.status:
            origin = "REJECTED_BY_RISK_GATE"
            not_taken_reason = res.message or "Blocked by guardrail"
        elif res.status in ("FILLED", "SUBMITTED", "DRY_RUN_SIMULATED"):
            origin = "EXECUTED"
            not_taken_reason = None
        else:
            origin = "EXECUTED"
            not_taken_reason = res.message if res.status == "FAILED" else None

        thesis_text = (
            f"Session: {session_id} | Playbook: {playbook} | "
            f"Status: {res.status} | Regime: {regime.posture if regime else 'N/A'}"
        )
        if prop and prop.rejection_reason:
            thesis_text += f" | Rejection: {prop.rejection_reason}"
        if tech:
            thesis_text += f" | RSI: {tech.rsi_14:.1f} | EMA Stack: {tech.ema_stack}"

        entry_price_override = res.filled_price if res.filled_price > 0 else (prop.limit_price if prop else None)
        target_price = prop.profit_target if prop else None
        stop_loss = prop.stop_loss if prop else None

        try:
            await log_conviction(
                ticker=ticker,
                direction=direction,
                confidence=4,
                reasoning=thesis_text,
                signals=f"RSI_{tech.rsi_state if tech else 'N/A'},EMA_{tech.ema_stack if tech else 'N/A'}",
                origin=origin,
                playbook=playbook,
                regime_posture=regime.posture if regime else None,
                session_id=session_id,
                not_taken_reason=not_taken_reason,
                target_price=target_price,
                stop_loss=stop_loss,
                entry_price_override=entry_price_override,
            )
            reflection_notes.append(
                f"Recorded conviction thesis for {ticker} ({res.status}, {direction}, origin={origin})"
            )
        except Exception as e:
            logger.warning(f"Failed to log conviction for {ticker}: {e}")
            reflection_notes.append(f"Thesis note for {ticker}: {thesis_text}")

    # ── 2. Log Proposals Rejected by User / Bypassed Execution ───────────────
    for prop in proposals:
        if prop.id in logged_prop_ids:
            continue
        logged_prop_ids.add(prop.id)
        logged_tickers.add(prop.ticker)

        direction = "bullish" if prop.side.upper() == "BUY" else "bearish"
        origin = "REJECTED_BY_USER"
        not_taken_reason = prop.rejection_reason or f"Human decision: {human_decision or 'REJECT'}"
        tech = technicals.get(prop.ticker)

        thesis_text = (
            f"Session: {session_id} | Playbook: {playbook} | "
            f"Status: REJECTED_BY_USER | Regime: {regime.posture if regime else 'N/A'} | "
            f"Reason: {not_taken_reason}"
        )
        if tech:
            thesis_text += f" | RSI: {tech.rsi_14:.1f} | EMA Stack: {tech.ema_stack}"

        try:
            await log_conviction(
                ticker=prop.ticker,
                direction=direction,
                confidence=3,
                reasoning=thesis_text,
                signals=f"RSI_{tech.rsi_state if tech else 'N/A'},EMA_{tech.ema_stack if tech else 'N/A'}",
                origin=origin,
                playbook=playbook,
                regime_posture=regime.posture if regime else None,
                session_id=session_id,
                not_taken_reason=not_taken_reason,
                target_price=prop.profit_target,
                stop_loss=prop.stop_loss,
                entry_price_override=prop.limit_price,
            )
            reflection_notes.append(
                f"Recorded non-executed proposal for {prop.ticker} ({direction}, origin={origin})"
            )
        except Exception as e:
            logger.warning(f"Failed to log user-rejected proposal for {prop.ticker}: {e}")

    # ── 3. Log Proposals Rejected by Risk Gate ───────────────────────────────
    for prop in rejected_proposals:
        if prop.id in logged_prop_ids:
            continue
        logged_prop_ids.add(prop.id)
        logged_tickers.add(prop.ticker)

        direction = "bullish" if prop.side.upper() == "BUY" else "bearish"
        origin = "REJECTED_BY_RISK_GATE"
        not_taken_reason = prop.rejection_reason or "Blocked by deterministic risk limits"
        tech = technicals.get(prop.ticker)

        thesis_text = (
            f"Session: {session_id} | Playbook: {playbook} | "
            f"Status: REJECTED_BY_RISK_GATE | Regime: {regime.posture if regime else 'N/A'} | "
            f"Reason: {not_taken_reason}"
        )
        if tech:
            thesis_text += f" | RSI: {tech.rsi_14:.1f} | EMA Stack: {tech.ema_stack}"

        try:
            await log_conviction(
                ticker=prop.ticker,
                direction=direction,
                confidence=3,
                reasoning=thesis_text,
                signals=f"RSI_{tech.rsi_state if tech else 'N/A'},EMA_{tech.ema_stack if tech else 'N/A'}",
                origin=origin,
                playbook=playbook,
                regime_posture=regime.posture if regime else None,
                session_id=session_id,
                not_taken_reason=not_taken_reason,
                target_price=prop.profit_target,
                stop_loss=prop.stop_loss,
                entry_price_override=prop.limit_price,
            )
            reflection_notes.append(
                f"Recorded risk-rejected proposal for {prop.ticker} ({direction}, origin={origin})"
            )
        except Exception as e:
            logger.warning(f"Failed to log risk-rejected proposal for {prop.ticker}: {e}")

    # ── 4. Lightweight Logging for Unproposed Candidates ─────────────────────
    for cand in candidates:
        if cand.ticker in logged_tickers:
            continue
        logged_tickers.add(cand.ticker)

        tech = technicals.get(cand.ticker)
        price_override = cand.data.get("price") or (tech.close if tech else None)
        confidence = max(1, min(5, int(round(cand.score / 20.0)))) if cand.score > 0 else 2
        not_taken_reason = "Candidate not selected for proposal synthesis"

        thesis_text = (
            f"Session: {session_id} | Candidate Source: {cand.source} | "
            f"Score: {cand.score:.1f} | Rationale: {cand.rationale}"
        )

        try:
            await log_conviction(
                ticker=cand.ticker,
                direction="bullish",
                confidence=confidence,
                reasoning=thesis_text,
                signals=f"Source_{cand.source},Score_{cand.score:.1f}",
                origin="NOT_PROPOSED",
                playbook=playbook,
                regime_posture=regime.posture if regime else None,
                session_id=session_id,
                not_taken_reason=not_taken_reason,
                entry_price_override=price_override,
            )
            reflection_notes.append(f"Recorded unproposed candidate {cand.ticker} (origin=NOT_PROPOSED)")
        except Exception as e:
            logger.debug(f"Skipped logging unproposed candidate {cand.ticker}: {e}")

    # ── 5. Auto-Resolve Open Convictions ─────────────────────────────────────
    try:
        res_summary = await resolve_convictions()
        if res_summary.get("resolved", 0) > 0:
            reflection_notes.append(
                f"Auto-resolved {res_summary['resolved']} open conviction horizons."
            )
    except Exception as e:
        logger.warning(f"Auto-resolution check failed in reflection_node: {e}")

    # ── 6. Log Multi-Agent Swarm Attribution ─────────────────────────────────
    worker_reports = state.get("worker_reports", {})
    debate_transcripts = state.get("debate_transcripts", [])
    if worker_reports:
        total_reports = sum(len(r) for r in worker_reports.values())
        reflection_notes.append(
            f"Swarm Attribution: {total_reports} reports across {len(worker_reports)} tickers evaluated."
        )
    if debate_transcripts:
        conflicts = sum(1 for d in debate_transcripts if d.get("has_conflict"))
        reflection_notes.append(
            f"Debate Attribution: {len(debate_transcripts)} debates conducted ({conflicts} arbitrated conflicts)."
        )

    audit_entry = {
        "node": "reflection_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "notes": reflection_notes,
    }

    return {
        "reflection_notes": reflection_notes,
        "audit_trail": [audit_entry],
    }

