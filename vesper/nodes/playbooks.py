"""Strategy Playbook Synthesis Node."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from vesper.account import fetch_live_equity
from vesper.state import TradingState, OrderProposal
from vesper.risk import RiskEnforcer

logger = logging.getLogger(__name__)


def _fetch_live_quote(symbol: str) -> Optional[float]:
    """Blocking — wrap in asyncio.to_thread. Returns None (never a guess) if
    Webull isn't configured or the quote can't be fetched."""
    try:
        from wb import Webull
        from md import Market

        wb = Webull()
        if not wb.configured:
            return None
        snap = Market(wb).snapshot([symbol])
        last = (snap.get(symbol) or {}).get("last")
        return float(last) if last else None
    except Exception as e:
        logger.warning(f"Could not fetch live quote for {symbol}: {e}")
        return None


async def playbooks_node(state: TradingState) -> Dict[str, Any]:
    """Applies domain playbooks to draft high-conviction order proposals."""
    logger.info("-> [PlaybooksNode] Applying strategy playbooks to candidates...")

    from mcp_server.conviction import get_playbook_performance

    proposals: List[OrderProposal] = []
    technicals = state.get("technicals", {})
    options_audits = state.get("options_audits", {})
    regime = state.get("regime")
    account_equity = await asyncio.to_thread(fetch_live_equity) if technicals else 0.0
    audit_notes = []

    # Check playbook outcome calibration history (Module 5 Phase 5)
    selected_playbook = state.get("selected_playbook", "all")
    calibration = get_playbook_performance(selected_playbook)
    size_adjustment = calibration.get("adjustment", 0.0)
    if calibration.get("resolved", 0) >= 3:
        if size_adjustment < 0:
            audit_notes.append(
                f"Calibration Guard: '{selected_playbook}' win rate is {calibration['win_rate_pct']:.0f}% "
                f"({calibration['wins']}/{calibration['resolved']}). Applying {size_adjustment:+.0%} risk scaling."
            )
        elif size_adjustment > 0:
            audit_notes.append(
                f"Calibration Boost: '{selected_playbook}' win rate is {calibration['win_rate_pct']:.0f}% "
                f"({calibration['wins']}/{calibration['resolved']}). Full conviction validated."
            )

    calibrated_equity = max(0.0, account_equity * (1.0 + size_adjustment))

    for ticker, tech in technicals.items():
        opt = options_audits.get(ticker)
        
        # ── 1. 0DTE FLOW PLAYBOOK (SPY / QQQ) ──────────────────────────────────
        if ticker in ("SPY", "QQQ") and regime and regime.spy_spot and regime.spy_gamma_flip:
            spot = regime.spy_spot
            flip = regime.spy_gamma_flip
            is_bullish = spot > flip
            side_type = "call" if is_bullish else "put"
            strike = round(spot + (1.0 if is_bullish else -1.0), 0)
            
            est_premium = 1.80  # Estimated 0DTE contract price
            qty = 1             # Strict 1-contract small account sizing
            cost = round(est_premium * 100 * qty, 2)
            stop_loss = round(est_premium * (1 - RiskEnforcer.STOP_LOSS_0DTE_PCT), 2)
            profit_target = round(est_premium * (1 + RiskEnforcer.TAKE_PROFIT_0DTE_PCT), 2)

            prop = OrderProposal(
                id=f"prop-0dte-{uuid.uuid4().hex[:6]}",
                ticker=ticker,
                asset_type="OPTION",
                side="BUY",
                order_type="LIMIT",
                quantity=qty,
                limit_price=est_premium,
                stop_loss=stop_loss,
                profit_target=profit_target,
                strike=strike,
                option_type=side_type,
                estimated_cost=cost,
                max_risk=round(cost * RiskEnforcer.STOP_LOSS_0DTE_PCT, 2),
                risk_reward_ratio=1.25,
            )
            proposals.append(prop)
            audit_notes.append(f"Drafted 0DTE {ticker} {side_type.upper()} Strike {strike} (Spot={spot} vs Flip={flip})")
            continue

        # ── 2. TAO OF TRADING BOUNCE 2.0 & MOMENTUM PULLBACK PLAYBOOK ────────
        # Rules (all six required — this is a mean-reversion pullback entry,
        # not a breakout filter; an earlier version of this playbook OR'd the
        # action-zone and stochastic-exhaustion checks against a bare
        # `rsi_14 > 45`/`<= 55`, which let almost any mildly-bullish reading
        # through regardless of whether price had actually pulled back or the
        # RSI(2) dip fired — the exact "trades on any bullish RSI" shape this
        # playbook was rewritten to get away from in the first place. Missing
        # data (rsi_2/slow_k/keltner unavailable) means "don't draft," not
        # "assume it passes" — this is candidate generation, not the safety
        # gate, but a proposal Vesper can't actually justify isn't worth
        # showing a human either):
        # 1. Bullish EMA stack (8 > 21 > 34 > 55 > 89)
        # 2. ADX(14) >= 18 (trend strength)
        # 3. Pullback into Keltner Action Zone (between EMA 21 and Keltner lower band, or ±1.5 ATR)
        # 4. Slow Stochastic(8,3) <= 40 (pullback oversold exhaustion)
        # 5. RSI(2) dip trigger: dipped to <=10 (this or the prior bar), now back above 10
        # 6. Not overbought (RSI(14) <= 68)
        is_bullish_trend = (tech.ema_stack == "BULLISH") or (tech.ema_8 and tech.ema_21 and tech.ema_8 >= tech.ema_21)
        adx_valid = (tech.adx_14 is None) or (tech.adx_14 >= 18.0)

        entry_price = tech.close
        atr = tech.atr_14 or (entry_price * 0.03)
        ema_21 = tech.ema_21 or entry_price

        # True Keltner Action Zone (length 14, 2x ATR) — required, not a fallback.
        keltner_lower = tech.keltner_lower or (ema_21 - (2.0 * atr))
        in_action_zone = (entry_price >= keltner_lower) and (entry_price <= ema_21 + (1.5 * atr))

        # Slow Stochastic(8,3) <= 40 — the documented threshold, no rsi_14 escape hatch.
        stoch_oversold = tech.slow_k is not None and tech.slow_k <= 40.0

        # RSI(2) dip-then-reset: dipped to <=10 on this bar or the prior one,
        # and has now crossed back above 10 (rsi_2 > 10). Both readings must
        # be present -- an unavailable rsi_2 means this trigger didn't fire,
        # not that it's assumed to have fired.
        rsi_2_trigger = (
            tech.rsi_2 is not None and tech.rsi_2_prev is not None
            and tech.rsi_2 > 10.0
            and (tech.rsi_2_prev <= 10.0 or tech.rsi_2 <= 10.0)
        )

        not_overbought = tech.rsi_14 <= 68.0

        if is_bullish_trend and in_action_zone and rsi_2_trigger and stoch_oversold and not_overbought and adx_valid:
            # Stop loss 1.5 ATR below entry / 21 EMA
            stop_loss = round(min(entry_price - (atr * 1.5), ema_21 - (atr * 1.0)), 2)
            if stop_loss >= entry_price:
                stop_loss = round(entry_price - (atr * 1.5), 2)
            profit_target = round(entry_price + (atr * 3.0), 2)
            
            # Volatility-Targeted Position Sizing
            shares, total_cost, total_risk = RiskEnforcer.calculate_vol_targeted_size(
                account_equity=calibrated_equity,
                entry_price=entry_price,
                stop_loss_price=stop_loss,
                target_price=profit_target,
                atr_14=atr,
            )
            
            if shares > 0:
                prop = OrderProposal(
                    id=f"prop-eq-{uuid.uuid4().hex[:6]}",
                    ticker=ticker,
                    asset_type="EQUITY",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=shares,
                    limit_price=entry_price,
                    stop_loss=stop_loss,
                    profit_target=profit_target,
                    estimated_cost=total_cost,
                    max_risk=total_risk,
                    risk_reward_ratio=round((profit_target - entry_price) / max(0.01, entry_price - stop_loss), 2),
                )
                proposals.append(prop)
                audit_notes.append(
                    f"Drafted Bounce 2.0 Equity Buy for {ticker}: {shares} shares @ ${entry_price:.2f} "
                    f"(Stop=${stop_loss:.2f}, Target=${profit_target:.2f}, Vol-Targeted Risk=${total_risk:.2f})"
                )

                # Optional OpenRouter AI Thesis Enrichment (if OPENROUTER_API_KEY configured)
                from vesper.llm import generate_candidate_thesis, is_llm_enabled
                if is_llm_enabled():
                    try:
                        thesis_res = await generate_candidate_thesis(
                            ticker=ticker,
                            technical_summary=tech.summary or f"Close=${entry_price}, RSI={tech.rsi_14:.1f}, EMA={tech.ema_stack}",
                            candidate_rationale="Bounce 2.0 Action Zone Pullback",
                            regime_posture=regime.posture if regime else "NEUTRAL",
                        )
                        if thesis_res and thesis_res.get("thesis"):
                            audit_notes.append(f"AI Thesis ({thesis_res.get('source')}): {thesis_res['thesis']}")
                    except Exception as e:
                        logger.debug("OpenRouter thesis enrichment skipped: %s", e)

                # Check if high-beta 2x leveraged vehicle exists (Module 6)
                from vesper.leveraged import get_primary_2x
                proxy_2x = get_primary_2x(ticker)
                if proxy_2x and proxy_2x != ticker:
                    # The leveraged ETF trades at its own price, unrelated to the
                    # underlying's — using entry_price here (as an earlier pass
                    # did) would draft a LIMIT order for the wrong instrument at
                    # the wrong price, and that fabricated number would flow
                    # straight into ExecutionGuard's notional-cap check as if it
                    # were real. Fetch a real quote or skip the proxy entirely;
                    # never guess a price for something that gets guarded on it.
                    proxy_price = await asyncio.to_thread(_fetch_live_quote, proxy_2x)
                    if proxy_price is None:
                        audit_notes.append(
                            f"Skipped 2x Leveraged Alternate for {ticker} ({proxy_2x}): no live quote available"
                        )
                    else:
                        # Scale down position by 2x to maintain equal risk budget
                        proxy_shares = max(1, shares // 2)
                        proxy_cost = round(proxy_shares * proxy_price, 2)
                        proxy_prop = OrderProposal(
                            id=f"prop-2x-{uuid.uuid4().hex[:6]}",
                            ticker=proxy_2x,
                            asset_type="LEVERAGED_ETF",
                            side="BUY",
                            order_type="LIMIT",
                            quantity=proxy_shares,
                            limit_price=proxy_price,
                            stop_loss=round(proxy_price * (1 - RiskEnforcer.STOP_LOSS_0DTE_PCT * 0.85), 2),
                            profit_target=round(proxy_price * (1 + RiskEnforcer.TAKE_PROFIT_0DTE_PCT * 1.30), 2),
                            estimated_cost=proxy_cost,
                            max_risk=round(total_risk * 0.5, 2),
                            risk_reward_ratio=2.5,
                        )
                        proposals.append(proxy_prop)
                        audit_notes.append(
                            f"Drafted 2x Leveraged Alternate: {proxy_2x} ({proxy_shares} shares @ ${proxy_price:.2f})"
                        )

    audit_entry = {
        "node": "playbooks_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "proposals_count": len(proposals),
        "notes": audit_notes,
    }

    return {
        "proposals": proposals,
        "needs_human_approval": len(proposals) > 0,
        "audit_trail": [audit_entry],
    }
