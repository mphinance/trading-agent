"""LangGraph Compiled Quant Trading StateGraph."""

from __future__ import annotations

import logging
from pathlib import Path

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from vesper import audit_chain
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

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CHECKPOINT_DB_PATH = _DATA_DIR / "checkpoints.sqlite"

# Process-lifetime singletons: AsyncSqliteSaver wants one open aiosqlite
# connection reused across every build_trading_graph() call, not a fresh
# connection (and a fresh `CREATE TABLE IF NOT EXISTS` via .setup()) each
# time a new graph is compiled -- which happens once per `vesper scan`/
# `vesper loop` scheduled scan, i.e. potentially many times per process.
_sqlite_conn = None
_sqlite_saver = None


async def _get_sqlite_checkpointer():
    """Lazily open (once per process) a persistent SQLite-backed
    checkpointer so a paused human-approval thread survives a restart.

    MemorySaver (langgraph's built-in default, and this module's previous
    and only option) loses every paused thread the moment the process exits
    -- acceptable for a single watched `vesper scan` run, a real gap now
    that `vesper loop` is a long-lived daemon that can crash or restart
    mid-approval-wait. Persisting the checkpoint alone isn't sufficient by
    itself: vesper/bot/inbound.py's ApprovalRegistry also had to become
    disk-backed for the same reason (see that module) -- resuming a
    thread_id requires knowing it exists in the first place.
    """
    global _sqlite_conn, _sqlite_saver
    if _sqlite_saver is not None:
        return _sqlite_saver

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _sqlite_conn = await aiosqlite.connect(str(_CHECKPOINT_DB_PATH))
    _sqlite_saver = AsyncSqliteSaver(_sqlite_conn)
    await _sqlite_saver.setup()
    return _sqlite_saver


def _with_audit_chain(node_name, node_fn):
    """Wrap a node so every `audit_trail` entry it returns is also committed
    to the hash-chained ledger (see `vesper/audit_chain.py`) the instant
    it's produced -- not batched at session end.

    Why per-node, not a session-end hook: `run_agent_session`'s own
    `final_state` (vesper/runner.py) only ever ends up holding the LAST
    node's own delta -- `app.astream()` yields per-node output dicts, not
    merged state -- so a session-end hook would silently chain only
    reflection_node's entry and lose every earlier one, the opposite of
    tamper-evidence. `human_gate_node` can also `interrupt()` and pause a
    thread across a process restart (the whole reason this graph has a
    persistent SQLite checkpointer), so a session may never reach a clean
    end within one process's lifetime at all. Per-node persistence commits
    each entry the instant it's produced, so a crash mid-approval-wait still
    leaves a gapless, verifiable chain up to whatever ran.

    Scope: post-trade, append-only, non-blocking. The try/except is the
    mechanism that makes "reports, never refuses" literally true -- a
    disk-full or permissions error is logged and swallowed here, and the
    node's real output still returns to the graph untouched. This never
    raises into, blocks, or alters the execution path.
    """
    async def _wrapped(state):
        output = await node_fn(state)
        if isinstance(output, dict) and output.get("audit_trail"):
            for entry in output["audit_trail"]:
                try:
                    audit_chain.append_entry(
                        session_id=state.get("session_id", "unknown"),
                        node=node_name,
                        entry=entry,
                    )
                except Exception:
                    logger.warning(
                        "audit_chain append failed for %s (non-fatal)", node_name, exc_info=True
                    )
        return output
    return _wrapped


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


async def build_trading_graph(checkpointer: bool = True, persistent: bool = True):
    """Build and compile the LangGraph quant trading engine.

    Async (unlike the old sync version) because a persistent checkpointer
    needs an awaited connection open. Callers: vesper/runner.py's
    run_agent_session (already async) and vesper.py's `listen`/`loop`
    command handlers (wrap the call in their own async entry point).

    persistent=True (default) uses the disk-backed SQLite checkpointer.
    persistent=False keeps the old in-memory-only MemorySaver -- for a
    caller that explicitly wants a throwaway graph (nothing in this repo
    currently needs that, but it's the honest equivalent of the old
    always-MemorySaver behavior rather than removing the option outright).
    """
    workflow = StateGraph(TradingState)

    # 1. Add all nodes, each wrapped so its audit_trail entries are
    # committed to the hash-chained ledger the instant they're produced
    # (see _with_audit_chain's docstring for why this is per-node wiring
    # rather than a session-end hook).
    workflow.add_node("regime_node", _with_audit_chain("regime_node", regime_node))
    workflow.add_node("scanner_node", _with_audit_chain("scanner_node", scanner_node))
    workflow.add_node("analyst_node", _with_audit_chain("analyst_node", analyst_node))
    workflow.add_node("playbooks_node", _with_audit_chain("playbooks_node", playbooks_node))
    workflow.add_node("risk_gate_node", _with_audit_chain("risk_gate_node", risk_gate_node))
    workflow.add_node("human_gate_node", _with_audit_chain("human_gate_node", human_gate_node))
    workflow.add_node("executor_node", _with_audit_chain("executor_node", executor_node))
    workflow.add_node("reflection_node", _with_audit_chain("reflection_node", reflection_node))

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
    if not checkpointer:
        memory = None
    elif persistent:
        memory = await _get_sqlite_checkpointer()
    else:
        memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)
    return app
