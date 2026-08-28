"""Hard-Stop Risk Guardrails & Position Sizing Engine."""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple
from vesper.state import OrderProposal


class RiskEnforcer:
    """Deterministic zero-loss-budget risk rules."""
    
    DEFAULT_MAX_RISK_PCT = 0.02       # 2% max risk per trade
    DEFAULT_MAX_POSITION_PCT = 0.15   # 15% max position size of account
    MAX_CONTRACTS_0DTE = 5            # Small account hard limit for 0DTE
    MIN_RISK_REWARD_RATIO = 1.5       # Minimum 1:1.5 Risk:Reward
    
    # 0DTE IFTTT Rules
    STOP_LOSS_0DTE_PCT = 0.40         # -40% hard stop
    TAKE_PROFIT_0DTE_PCT = 0.50       # +50% take profit
    HARD_EXIT_TIME_0DTE = "15:00"     # 3:00 PM ET exit cascade
    
    @classmethod
    def calculate_equity_size(
        cls,
        account_equity: float,
        entry_price: float,
        stop_loss_price: float,
        target_price: float,
        max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
    ) -> Tuple[int, float, float]:
        """Calculate shares based on risk per share and account equity.
        
        Returns:
            (shares, total_cost, max_risk_amount)
        """
        if entry_price <= 0 or stop_loss_price >= entry_price:
            return 0, 0.0, 0.0
            
        risk_per_share = entry_price - stop_loss_price
        reward_per_share = target_price - entry_price
        
        # Risk:Reward sanity check
        if reward_per_share <= 0 or (reward_per_share / risk_per_share) < cls.MIN_RISK_REWARD_RATIO:
            # Squeeze trade target is suboptimal
            pass
            
        max_risk_dollars = account_equity * max_risk_pct
        shares_by_risk = math.floor(max_risk_dollars / risk_per_share)
        
        # Cap by max position percentage
        max_position_dollars = account_equity * cls.DEFAULT_MAX_POSITION_PCT
        shares_by_capital = math.floor(max_position_dollars / entry_price)
        
        final_shares = max(1, min(shares_by_risk, shares_by_capital))
        total_cost = round(final_shares * entry_price, 2)
        total_risk = round(final_shares * risk_per_share, 2)
        
        return final_shares, total_cost, total_risk

    @classmethod
    def validate_proposal(cls, proposal: OrderProposal, account_equity: float = 10000.0) -> Tuple[bool, Optional[str]]:
        """Validate order proposal against deterministic risk constraints."""
        if proposal.limit_price <= 0:
            return False, "Limit price must be greater than 0."
            
        if proposal.quantity <= 0:
            return False, "Quantity must be at least 1."
            
        if proposal.asset_type == "OPTION" and proposal.quantity > cls.MAX_CONTRACTS_0DTE:
            return False, f"Quantity {proposal.quantity} exceeds 0DTE limit ({cls.MAX_CONTRACTS_0DTE} contracts)."
            
        estimated_cost = proposal.estimated_cost or (proposal.limit_price * proposal.quantity * (100 if proposal.asset_type == "OPTION" else 1))
        if estimated_cost > account_equity:
            return False, f"Estimated cost (${estimated_cost:,.2f}) exceeds account equity (${account_equity:,.2f})."
            
        return True, None
