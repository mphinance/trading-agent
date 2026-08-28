"""Strategy Playbook Synthesis Node."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.state import TradingState, OrderProposal
from vesper.risk import RiskEnforcer

logger = logging.getLogger(__name__)


async def playbooks_node(state: TradingState) -> Dict[str, Any]:
    """Applies domain playbooks to draft high-conviction order proposals."""
    logger.info("-> [PlaybooksNode] Applying strategy playbooks to candidates...")
    
    proposals: List[OrderProposal] = []
    technicals = state.get("technicals", {})
    options_audits = state.get("options_audits", {})
    regime = state.get("regime")
    account_equity = 10000.0  # Default base equity
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
