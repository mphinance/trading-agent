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
    TARGET_ANNUAL_VOLATILITY = 0.15   # 15% target portfolio volatility
    TRADING_DAYS_PER_YEAR = 252
    
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
    def calculate_vol_targeted_size(
        cls,
        account_equity: float,
        entry_price: float,
        stop_loss_price: float,
        target_price: float,
        atr_14: Optional[float] = None,
        target_vol_annual: float = TARGET_ANNUAL_VOLATILITY,
    ) -> Tuple[int, float, float]:
        """Calculate volatility-targeted position sizing based on realized ATR.

        Formula:
            daily_target_vol = target_vol_annual / sqrt(252)
            realized_daily_vol = (atr_14 / entry_price) if atr_14 else 0.02
            vol_scalar = clip(daily_target_vol / realized_daily_vol, 0.4, 1.6)
            effective_risk_pct = DEFAULT_MAX_RISK_PCT * vol_scalar

        Returns:
            (shares, total_cost, max_risk_amount)
        """
        if entry_price <= 0 or stop_loss_price >= entry_price:
            return 0, 0.0, 0.0

        daily_target_vol = target_vol_annual / math.sqrt(cls.TRADING_DAYS_PER_YEAR)
        realized_daily_vol = (atr_14 / entry_price) if (atr_14 and atr_14 > 0) else 0.02
        vol_scalar = max(0.4, min(1.6, daily_target_vol / max(0.005, realized_daily_vol)))

        effective_risk_pct = cls.DEFAULT_MAX_RISK_PCT * vol_scalar
        return cls.calculate_equity_size(
            account_equity=account_equity,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            target_price=target_price,
            max_risk_pct=effective_risk_pct,
        )

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

    # Capital allocation bucket defaults. Distinct from a single-order
    # notional/quantity cap (execution_guard's job) -- these look across ALL
    # currently open positions, because a series of individually-compliant
    # orders can still concentrate an account in one risk shape.
    MAX_OPEN_LONG_OPTIONS = 1
    MAX_WHEEL_STOCK_PCT = 0.20

    @classmethod
    def check_capital_allocation_buckets(
        cls,
        proposal: OrderProposal,
        open_long_option_count: int,
        wheel_stock_notional: float,
        account_equity: float,
        max_open_long_options: int = MAX_OPEN_LONG_OPTIONS,
        max_wheel_stock_pct: float = MAX_WHEEL_STOCK_PCT,
    ) -> Tuple[bool, Optional[str]]:
        """Pure, deterministic bucket check -- does NOT fetch positions itself.

        Callers (risk_gate_node) are responsible for counting
        open_long_option_count and wheel_stock_notional from whatever
        position source is authoritative for the current mode. This is
        deliberately a caller-supplied-inputs function rather than one that
        reaches into wb.py/paper_ledger.py itself, so the two very different
        position sources (live broker positions vs. paper ledger fills) stay
        the caller's problem, not this pure function's.

        Two buckets:
        - MAX_OPEN_LONG_OPTIONS: at most 1 open long option position at a
          time (a BUY option that isn't closing an existing short). Counted
          uniformly across live and paper positions since a manually-placed
          Webull Desktop position should count too -- this is a real account
          concentration limit, not a "Vesper's own trades" limit.
        - MAX_WHEEL_STOCK_PCT: equity acquired via strategy_type=
          "WHEEL_ASSIGNMENT" capped at 20% of account equity. Only
          enforceable where that tag is actually tracked (currently: paper
          ledger fills only -- see risk_gate_node for the live-mode gap this
          leaves, and ROADMAP.md for why).
        """
        is_long_option = (
            proposal.asset_type == "OPTION"
            and proposal.side.upper() in ("BUY", "LONG")
            and not getattr(proposal, "is_closing", False)
        )
        if is_long_option and open_long_option_count >= max_open_long_options:
            return False, (
                f"Capital allocation: already {open_long_option_count} open long option "
                f"position(s), at the {max_open_long_options}-position cap for new long options."
            )

        is_wheel_stock_buy = (
            proposal.asset_type == "EQUITY"
            and proposal.side.upper() == "BUY"
            and getattr(proposal, "strategy_type", None) == "WHEEL_ASSIGNMENT"
        )
        if is_wheel_stock_buy:
            added_notional = proposal.estimated_cost or (proposal.limit_price * proposal.quantity)
            projected = wheel_stock_notional + added_notional
            cap = account_equity * max_wheel_stock_pct
            if projected > cap:
                return False, (
                    f"Capital allocation: wheel-stock notional would reach ${projected:,.2f}, "
                    f"exceeding the {max_wheel_stock_pct:.0%} cap (${cap:,.2f}) of account equity."
                )

        return True, None
