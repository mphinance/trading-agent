"""Vesper Multi-Agent Swarm & Specialist Analyst Suite."""

from __future__ import annotations

from vesper.agents.base import BaseSpecialistAgent
from vesper.agents.technical import TechnicalAnalystAgent
from vesper.agents.flow import InstitutionalFlowAgent
from vesper.agents.fundamental import FundamentalAgent
from vesper.agents.gamma import GammaStructureAgent
from vesper.agents.supervisor import MacroSupervisor
from vesper.agents.synthesis import DebateSynthesisSupervisor
from vesper.agents.risk_adversary import AdversarialRiskAgent

__all__ = [
    "BaseSpecialistAgent",
    "TechnicalAnalystAgent",
    "InstitutionalFlowAgent",
    "FundamentalAgent",
    "GammaStructureAgent",
    "MacroSupervisor",
    "DebateSynthesisSupervisor",
    "AdversarialRiskAgent",
]
