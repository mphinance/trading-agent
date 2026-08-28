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

    proposals: List[OrderProposal] = []
    technicals = state.get("technicals", {})
    options_audits = state.get("options_audits", {})
    regime = state.get("regime")
    account_equity = await asyncio.to_thread(fetch_live_equity) if technicals else 0.0
    audit_notes = []

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

        # ── 2. MOMENTUM SQUEEZE & VCP PLAYBOOK (EQUITY) ───────────────────────
        if tech.ema_stack == "BULLISH" or tech.rsi_14 > 50:
            entry_price = tech.close
            # Stop loss just below EMA 21 or 1.5 ATR
            atr = tech.atr_14 or (entry_price * 0.03)
            stop_loss = round(entry_price - (atr * 1.5), 2)
            profit_target = round(entry_price + (atr * 3.0), 2)
            
            shares, total_cost, total_risk = RiskEnforcer.calculate_equity_size(
                account_equity=account_equity,
                entry_price=entry_price,
                stop_loss_price=stop_loss,
                target_price=profit_target,
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
                    risk_reward_ratio=2.0,
                )
                proposals.append(prop)
                audit_notes.append(f"Drafted Equity Buy for {ticker}: {shares} shares @ ${entry_price:.2f} (Risk=${total_risk:.2f})")

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
