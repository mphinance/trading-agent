"""Adversarial Risk & Red-Teaming Specialist Agent."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from vesper.state import TradingState, OrderProposal

logger = logging.getLogger(__name__)


class AdversarialRiskAgent:
    """Adversarial agent stress-testing trade theses to surface hidden vulnerabilities."""

    name: str = "risk_adversary_agent"

    def red_team_proposal(self, proposal: OrderProposal, state: TradingState) -> Dict[str, Any]:
        """Perform adversarial critique on a drafted OrderProposal.

        Returns:
            Dict with 'verdict' ('CLEARED' or 'WARNING'), 'risk_flags', and 'counter_thesis'.
        """
        flags: List[str] = []
        counter_thesis: List[str] = []

        # 1. Check risk-reward profile
        if proposal.risk_reward_ratio > 0 and proposal.risk_reward_ratio < 1.5:
            flags.append(f"Sub-optimal Risk/Reward ratio ({proposal.risk_reward_ratio:.2f}:1 < 1.5:1 minimum standard)")
            counter_thesis.append("Asymmetric downside risk relative to expected target gain.")

        # 2. Check for naked/undefined risk in options
        if proposal.asset_type == "OPTION" and proposal.side == "SELL" and not proposal.legs:
            flags.append("Naked short option exposure detected without hedge leg")
            counter_thesis.append("Tail-risk assignment hazard in volatile market conditions.")

        # 3. Check macro regime alignment
        regime = state.get("regime")
        if regime and regime.posture in ("DEFENSIVE", "HIGH_RISK_DISTRIBUTION"):
            if proposal.side == "BUY" and proposal.asset_type == "EQUITY":
                flags.append(f"Long equity purchase during {regime.posture} macro posture")
                counter_thesis.append("High probability of broad market beta drag overwhelming individual alpha.")

        # 4. Check missing stop loss on directional positions
        if proposal.side == "BUY" and proposal.stop_loss is None and proposal.asset_type == "EQUITY":
            flags.append("Missing explicit hard stop level on long equity position")
            counter_thesis.append("Uncapped drawdown vulnerability on gap-down events.")

        verdict = "WARNING" if len(flags) > 0 else "CLEARED"

        return {
            "proposal_id": proposal.id,
            "ticker": proposal.ticker,
            "verdict": verdict,
            "risk_flags": flags,
            "counter_thesis": " ".join(counter_thesis) if counter_thesis else "No severe adversarial flaws detected.",
        }
