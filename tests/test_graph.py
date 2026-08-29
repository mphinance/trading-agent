"""Tests for vesper/graph.py's persistent (SQLite-backed) checkpointer.

The property under test: a paused human-approval thread must survive a
process restart, not just live for the lifetime of one Python object. A
minimal standalone graph (not the full trading graph) is used so this test
exercises the checkpointer mechanism itself, not the trading graph's
business logic (already covered by test_execution_integration.py and every
individual node's own tests).
"""

from __future__ import annotations

import pytest
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from typing_extensions import TypedDict

from vesper.graph import _get_sqlite_checkpointer, build_trading_graph


class _MiniState(TypedDict):
    value: str


async def _pause_node(state: _MiniState) -> dict:
    decision = interrupt({"prompt": "waiting for approval"})
    return {"value": f"resumed:{decision}"}


async def _build_mini_graph(checkpointer):
    workflow = StateGraph(_MiniState)
    workflow.add_node("pause_node", _pause_node)
    workflow.add_edge(START, "pause_node")
    workflow.add_edge("pause_node", END)
    return workflow.compile(checkpointer=checkpointer)


@pytest.mark.asyncio
async def test_paused_thread_survives_a_simulated_process_restart():
    """The core property this whole fix exists for: a fresh graph object
    built against a REOPENED sqlite connection (simulating a new process
    starting up) can still resume a thread an earlier graph object paused.
    Reusing the same in-process connection wouldn't prove this -- an
    in-memory checkpointer would pass that version of the test too."""
    import vesper.graph as graph_module

    checkpointer1 = await _get_sqlite_checkpointer()
    app1 = await _build_mini_graph(checkpointer1)
    config = {"configurable": {"thread_id": "test-thread-restart"}}

    result1 = await app1.ainvoke({"value": "start"}, config=config)
    assert "__interrupt__" in result1  # paused, not completed

    # Force a genuine reopen from disk (simulating a process restart) rather
    # than reusing the cached singleton connection.
    await graph_module._sqlite_conn.close()
    graph_module._sqlite_conn = None
    graph_module._sqlite_saver = None

    checkpointer2 = await _get_sqlite_checkpointer()
    app2 = await _build_mini_graph(checkpointer2)
    result2 = await app2.ainvoke(Command(resume="APPROVE"), config=config)

    assert result2["value"] == "resumed:APPROVE"


@pytest.mark.asyncio
async def test_get_sqlite_checkpointer_is_a_process_lifetime_singleton():
    """Repeated calls within the same process must reuse the same
    connection/saver, not open a new sqlite connection per graph build --
    build_trading_graph() is called once per scan, which could be many
    times per long-running `vesper loop` process."""
    saver1 = await _get_sqlite_checkpointer()
    saver2 = await _get_sqlite_checkpointer()
    assert saver1 is saver2


@pytest.mark.asyncio
async def test_build_trading_graph_default_uses_persistent_checkpointer():
    app = await build_trading_graph(checkpointer=True)
    expected_saver = await _get_sqlite_checkpointer()
    assert app.checkpointer is expected_saver


@pytest.mark.asyncio
async def test_build_trading_graph_persistent_false_uses_memory_saver():
    """The explicit escape hatch for a throwaway, non-persistent graph."""
    from langgraph.checkpoint.memory import MemorySaver

    app = await build_trading_graph(checkpointer=True, persistent=False)
    assert isinstance(app.checkpointer, MemorySaver)


@pytest.mark.asyncio
async def test_build_trading_graph_checkpointer_false_disables_checkpointing():
    app = await build_trading_graph(checkpointer=False)
    assert app.checkpointer is None
