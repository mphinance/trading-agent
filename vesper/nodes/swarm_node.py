"""Parallel Multi-Agent Specialist Swarm Node."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from vesper.state import TradingState, WorkerReport
from vesper.agents import (
    TechnicalAnalystAgent,
    InstitutionalFlowAgent,
    FundamentalAgent,
    GammaStructureAgent,
    MacroSupervisor,
)

logger = logging.getLogger(__name__)


async def swarm_node(state: TradingState) -> Dict[str, Any]:
    """Dispatches parallel specialist agents across top candidate tickers."""
    candidates = state.get("candidates", [])
    logger.info(f"-> [SwarmNode] Deploying specialist worker swarm across {len(candidates)} candidate(s)...")

    supervisor = MacroSupervisor()
    active_worker_names = supervisor.select_active_workers(state)

    # Instantiate the agent map
    agent_instances = {
        "technical_agent": TechnicalAnalystAgent(),
        "flow_agent": InstitutionalFlowAgent(),
        "fundamental_agent": FundamentalAgent(),
        "gamma_agent": GammaStructureAgent(),
    }

    workers_to_run = [agent_instances[name] for name in active_worker_names if name in agent_instances]

    # Evaluate top candidates (up to top 5)
    target_candidates = candidates[:5] if candidates else []
    worker_reports: Dict[str, List[WorkerReport]] = {}
    audit_entries: List[Dict[str, Any]] = []

    async def _run_agent_on_ticker(agent, ticker: str) -> WorkerReport:
        try:
            return await agent.analyze(ticker=ticker, state=state)
        except Exception as e:
            logger.warning(f"[SwarmNode] Agent {agent.name} failed on {ticker}: {e}")
            return WorkerReport(
                agent_name=agent.name,
                ticker=ticker,
                direction="NEUTRAL",
                confidence_score=30.0,
                thesis_summary=f"Analysis error: {e}",
            )

    # Build tasks for all (agent, candidate) pairs to run concurrently
    tasks = []
    task_keys = []
    for c in target_candidates:
        t = c.ticker
        for agent in workers_to_run:
            tasks.append(_run_agent_on_ticker(agent, t))
            task_keys.append(t)

    if tasks:
        results = await asyncio.gather(*tasks)
        for ticker, report in zip(task_keys, results):
            if ticker not in worker_reports:
                worker_reports[ticker] = []
            worker_reports[ticker].append(report)

    # Build audit trail entry
    total_reports = sum(len(r) for r in worker_reports.values())
    audit_entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": "SWARM_ANALYSIS",
        "active_workers": active_worker_names,
        "tickers_analyzed": list(worker_reports.keys()),
        "total_reports_generated": total_reports,
    })

    logger.info(f"-> [SwarmNode] Swarm complete: Generated {total_reports} reports across {len(worker_reports)} tickers.")

    return {
        "worker_reports": worker_reports,
        "active_workers": active_worker_names,
        "audit_trail": audit_entries,
    }
