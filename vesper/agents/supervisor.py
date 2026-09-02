"""Macro & Strategy Dispatcher Supervisor."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from vesper.state import TradingState, MarketRegime

logger = logging.getLogger(__name__)


class MacroSupervisor:
    """Supervisor coordinating macro strategy dispatch and active specialist worker selection."""

    def __init__(self):
        self.all_workers = [
            "technical_agent",
            "flow_agent",
            "fundamental_agent",
            "gamma_agent",
        ]

    def select_active_workers(self, state: TradingState) -> List[str]:
        """Dynamically select which specialist agents to deploy based on regime and target playbooks."""
        regime: Optional[MarketRegime] = state.get("regime")
        selected_playbook = state.get("selected_playbook", "all")

        # In specific focused playbook runs, prioritize relevant specialists
        if selected_playbook == "0dte_flow":
            return ["gamma_agent", "flow_agent", "technical_agent"]
        if selected_playbook in ("thega", "collar_following"):
            return ["fundamental_agent", "technical_agent", "flow_agent"]
        if selected_playbook == "earnings_vega_harvest":
            return ["fundamental_agent", "gamma_agent", "flow_agent"]

        # In defensive/high risk distribution, always run full panel to cross-validate defensively
        if regime and regime.posture in ("DEFENSIVE", "HIGH_RISK_DISTRIBUTION"):
            logger.info("[MacroSupervisor] Defensive regime detected: Activating full multi-agent panel for rigorous verification.")
            return list(self.all_workers)

        return list(self.all_workers)

    def filter_eligible_playbooks(self, state: TradingState) -> List[str]:
        """Determine which playbooks are permitted in the current macro context."""
        regime: Optional[MarketRegime] = state.get("regime")
        posture = regime.posture if regime else "NEUTRAL"

        if posture in ("DEFENSIVE", "HIGH_RISK_DISTRIBUTION"):
            # Restrict to defensive or volatility-selling playbooks
            return ["collar_following", "thega", "earnings_vega_harvest", "volatility_harvester"]
        
        return [
            "0dte_flow",
            "momentum_squeeze",
            "tao_bounce",
            "thega",
            "collar_following",
            "adx_iv_router",
            "earnings_vega_harvest",
        ]
