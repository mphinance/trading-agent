"""LangGraph Compiled Quant Trading StateGraph."""

from __future__ import annotations

import logging
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from vesper.state import TradingState
from vesper.nodes import (
    regime_node,
    scanner_node,
    analyst_node,
    playbooks_node,
    risk_gate_node,
    human_gate_node,
    executor_node,
    reflection_node,
)

logger = logging.getLogger(__name__)


def should_execute_route(state: TradingState) -> str:
    """Conditional router after Human Gate."""
    proposals = state.get("proposals", [])
    decision = state.get("human_decision", "")
    
    if not proposals:
        logger.info("[Router] No valid proposals. Routing to reflection.")
        return "reflection_node"
        
    if decision in ("APPROVE", "AUTO_DRY_RUN"):
        logger.info("[Router] Order approved. Routing to executor.")
        return "executor_node"
        
    logger.info(f"[Router] Order decision is '{decision}'. Routing to reflection.")
    return "reflection_node"


def build_trading_graph(checkpointer: bool = True):
    """Build and compile the LangGraph quant trading engine."""
    workflow = StateGraph(TradingState)

    # 1. Add all nodes
    workflow.add_node("regime_node", regime_node)
    workflow.add_node("scanner_node", scanner_node)
    workflow.add_node("analyst_node", analyst_node)
    workflow.add_node("playbooks_node", playbooks_node)
    workflow.add_node("risk_gate_node", risk_gate_node)
    workflow.add_node("human_gate_node", human_gate_node)
    workflow.add_node("executor_node", executor_node)
    workflow.add_node("reflection_node", reflection_node)

    # 2. Add sequential edges
    workflow.add_edge(START, "regime_node")
    workflow.add_edge("regime_node", "scanner_node")
    workflow.add_edge("scanner_node", "analyst_node")
    workflow.add_edge("analyst_node", "playbooks_node")
    workflow.add_edge("playbooks_node", "risk_gate_node")
    workflow.add_edge("risk_gate_node", "human_gate_node")

    # 3. Conditional Edge after Human Gate
    workflow.add_conditional_edges(
        "human_gate_node",
        should_execute_route,
        {
            "executor_node": "executor_node",
            "reflection_node": "reflection_node",
        },
    )

    workflow.add_edge("executor_node", "reflection_node")
    workflow.add_edge("reflection_node", END)

    # 4. Compile with checkpointer
    memory = MemorySaver() if checkpointer else None
    app = workflow.compile(checkpointer=memory)
    return app
