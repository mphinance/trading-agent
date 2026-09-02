# Vesper: Dynamic Multi-Agent Workflow Plan

This document outlines the step-by-step implementation plan for transitioning Vesper from a single-agent linear flow to a dynamic, multi-agent swarm architecture inspired by HKUDS's `ClawTeam` and `AI-Trader`. 

Based on an analysis of the current `vesper/graph.py` architecture, here are the 5 exact integration points where the Supervisor pattern will be injected:

## Integration Point 1: Macro & Strategy Dispatcher Supervisor (Augmenting `regime_node`)
**Goal:** Replace static regime evaluation with a dynamic dispatcher.
- **Current State:** `regime_node` statically evaluates market health to output a posture label (`BULLISH`, `DEFENSIVE`, etc.).
- **Supervisor Pattern:** The Macro Supervisor evaluates VIX, dealer flip levels, and macro trends to dynamically select which scanning pipelines and playbooks should be activated. In defensive regimes, it dispatches only defensive workers (e.g., Cash-Secured Puts, Volatility Harvester), bypassing aggressive long breakout scanners.

## Integration Point 2: Parallel Specialist Worker Swarm (Replacing `scanner_node` + `analyst_node`)
**Goal:** Fan-out candidate discovery and deep audits across specialized agents.
- **Current State:** `scanner_node` runs 8 preset scans sequentially; `analyst_node` runs technical indicator calculations sequentially.
- **Supervisor Pattern:** Distribute discovery across parallel workers:
  1. **Technical Analyst Agent:** Technicals, Minervini VCP screening, EMA stacks, bounce signals.
  2. **Institutional & Flow Agent:** Whales, 13F changes, unusual options flow, dark pool activity.
  3. **Fundamental Agent:** SEC filings (`edgar.py`), earnings calendar, PEAD, balance sheet.
  4. **0DTE & Gamma Agent:** Apex levels, GEX gamma flip, 0DTE options chains.

## Integration Point 3: Debate & Synthesis Supervisor (Replacing monolithic `playbooks_node`)
**Goal:** Resolve conflicting signals from experts.
- **Current State:** `playbooks_node` uses hardcoded rule ladders (0DTE, Tao Bounce, etc.) and a single prompt to generate a thesis.
- **Supervisor Pattern (Portfolio Manager):** Identifies conflicting worker outputs (e.g., Technical is Bullish, Fundamental is Bearish). It routes conflicting cases through an internal debate loop, weighs trade horizons, selects the optimal playbook, and compiles the formal `OrderProposal`.

## Integration Point 4: Adversarial Risk Supervisor (Augmenting `risk_gate_node`)
**Goal:** Multi-agent red-teaming and dynamic sizing.
- **Current State:** `risk_gate_node` executes deterministic rules followed by a single LLM self-critique prompt.
- **Supervisor Pattern:** Coordinates:
  1. **Deterministic Guardrails:** (Non-negotiable) Hard capital caps, max 2% risk, circuit breakers.
  2. **Adversarial Red-Team Agent:** Actively attempts to invalidate the trade thesis.
  3. **Dynamic Capital Allocator:** Scales position sizing based on historical agent performance weights.

## Integration Point 5: Reflection, Leaderboard & Skill Evolution (Augmenting `reflection_node`)
**Goal:** Track success and autonomously evolve skills.
- **Current State:** `reflection_node` logs trade conviction and triggers horizon resolution.
- **Supervisor Pattern:** 
  - **Attribution Engine:** Records which worker agents contributed to each proposal and updates an `agent_performance` leaderboard when trades close.
  - **Skill Evolution Node:** Uses `vesper/skills_engine.py` to autonomously write and update markdown skill playbooks based on post-trade lessons learned.

---

## Required State Adjustments (`vesper/state.py`)

To support this cleanly, expand `TradingState` TypedDict:
```python
class WorkerReport(BaseModel):
    agent_name: str
    ticker: str
    direction: str  # "BULLISH", "BEARISH", "NEUTRAL"
    confidence_score: float  # 0.0 to 100.0
    time_horizon: str  
    key_catalysts: List[str] = Field(default_factory=list)
    invalidation_levels: List[float] = Field(default_factory=list)
    data: Dict[str, Any] = Field(default_factory=dict)

# Add to TradingState:
worker_reports: Annotated[Dict[str, List[WorkerReport]], operator.ior]
active_workers: List[str]
debate_transcripts: Annotated[List[Dict[str, Any]], operator.add]
agent_conviction_weights: Dict[str, float]
```

## Graph Wiring Invariants (`vesper/graph.py`)
- Replace linear edges with LangGraph dynamic conditional routing (`Send` API) from the Supervisor to parallel worker nodes, collecting results into the Synthesis/PM node before passing to `risk_gate_node`.
- **CRITICAL:** Keep `_with_audit_chain` on all new worker nodes.
- **CRITICAL:** Keep `AsyncSqliteSaver` persistent checkpointer and `human_gate_node` `interrupt()` mechanism for Telegram approvals.
- **CRITICAL:** Keep deterministic hard boundaries in `execution_guard.py`.
```
