"""Tests for vesper/audit_chain.py's hash-chained, tamper-evident ledger,
and vesper/graph.py's per-node wiring of it (_with_audit_chain).

Two properties matter most and get their own tests:
1. The chain detects tampering (edited content, a deleted middle entry)
   and localizes it, per verify_chain()'s documented break_reason shapes.
2. A chain-write failure never propagates into the graph's execution path
   -- "reports, never refuses" (see graph.py's _with_audit_chain docstring).
"""

from __future__ import annotations

import json

import pytest
from typing_extensions import TypedDict

from vesper import audit_chain


def _read_lines():
    if not audit_chain._CHAIN_PATH.exists():
        return []
    with open(audit_chain._CHAIN_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_genesis_record_prev_hash_is_the_genesis_constant():
    record = audit_chain.append_entry("session-1", "regime_node", {"posture": "BULLISH"})
    assert record["index"] == 0
    assert record["prev_hash"] == audit_chain.GENESIS_HASH


def test_three_appends_chain_correctly():
    r0 = audit_chain.append_entry("session-1", "regime_node", {"a": 1})
    r1 = audit_chain.append_entry("session-1", "scanner_node", {"b": 2})
    r2 = audit_chain.append_entry("session-1", "analyst_node", {"c": 3})

    assert r1["prev_hash"] == r0["hash"]
    assert r2["prev_hash"] == r1["hash"]
    assert r0["index"] == 0 and r1["index"] == 1 and r2["index"] == 2


def test_verify_chain_passes_on_untouched_chain():
    for i in range(5):
        audit_chain.append_entry("session-1", f"node-{i}", {"i": i})

    result = audit_chain.verify_chain()
    assert result == {"valid": True, "entry_count": 5}


def test_verify_chain_on_missing_file():
    assert not audit_chain._CHAIN_PATH.exists()
    assert audit_chain.verify_chain() == {"valid": True, "entry_count": 0}


def test_verify_chain_on_empty_file():
    audit_chain._DATA_DIR.mkdir(parents=True, exist_ok=True)
    audit_chain._CHAIN_PATH.write_text("")
    assert audit_chain.verify_chain() == {"valid": True, "entry_count": 0}


def test_verify_chain_detects_an_edited_entry():
    audit_chain.append_entry("session-1", "regime_node", {"posture": "BULLISH"})
    audit_chain.append_entry("session-1", "scanner_node", {"candidates": 3})
    audit_chain.append_entry("session-1", "analyst_node", {"rsi": 55})

    lines = audit_chain._CHAIN_PATH.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["entry"] = {"candidates": 999}  # content edited; hash NOT recomputed
    lines[1] = json.dumps(tampered, sort_keys=True, default=str)
    audit_chain._CHAIN_PATH.write_text("\n".join(lines) + "\n")

    result = audit_chain.verify_chain()
    assert result["valid"] is False
    assert result["break_index"] == 1
    assert result["break_node"] == "scanner_node"
    assert "hash" in result["break_reason"]


def test_verify_chain_detects_a_deleted_middle_entry():
    audit_chain.append_entry("session-1", "regime_node", {"a": 1})
    audit_chain.append_entry("session-1", "scanner_node", {"b": 2})
    audit_chain.append_entry("session-1", "analyst_node", {"c": 3})

    lines = audit_chain._CHAIN_PATH.read_text().splitlines()
    del lines[1]  # remove the middle entry
    audit_chain._CHAIN_PATH.write_text("\n".join(lines) + "\n")

    result = audit_chain.verify_chain()
    assert result["valid"] is False
    assert result["break_index"] == 1
    assert result["break_node"] == "analyst_node"  # the record that used to follow it
    assert "prev_hash" in result["break_reason"]


def test_digest_is_stable_and_content_sensitive():
    a = audit_chain._digest({"x": 1, "y": 2})
    b = audit_chain._digest({"y": 2, "x": 1})  # key order must not matter
    c = audit_chain._digest({"x": 1, "y": 3})
    assert a == b
    assert a != c


class TestGraphWrapper:
    """Exercises vesper/graph.py's _with_audit_chain in isolation, without
    the cost of driving all 8 real nodes' live dependencies."""

    @pytest.mark.asyncio
    async def test_wrapper_appends_every_audit_trail_entry(self):
        from vesper.graph import _with_audit_chain

        async def fake_node(state):
            return {"audit_trail": [{"node": "fake_node", "value": 1}]}

        wrapped = _with_audit_chain("fake_node", fake_node)
        output = await wrapped({"session_id": "sess-abc"})

        assert output == {"audit_trail": [{"node": "fake_node", "value": 1}]}
        result = audit_chain.verify_chain()
        assert result == {"valid": True, "entry_count": 1}

    @pytest.mark.asyncio
    async def test_wrapper_never_propagates_an_append_failure(self, monkeypatch):
        """The single test that pins 'reports, never refuses': a chain-write
        failure must not raise out of the wrapped node, and the node's own
        real output must reach the graph unchanged."""
        from vesper.graph import _with_audit_chain

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(audit_chain, "append_entry", _boom)

        async def fake_node(state):
            return {"audit_trail": [{"node": "fake_node", "value": 42}]}

        wrapped = _with_audit_chain("fake_node", fake_node)
        output = await wrapped({"session_id": "sess-abc"})  # must not raise

        assert output == {"audit_trail": [{"node": "fake_node", "value": 42}]}

    @pytest.mark.asyncio
    async def test_wrapper_is_a_noop_when_node_returns_no_audit_trail(self):
        from vesper.graph import _with_audit_chain

        async def fake_node(state):
            return {"some_other_key": True}

        wrapped = _with_audit_chain("fake_node", fake_node)
        output = await wrapped({"session_id": "sess-abc"})

        assert output == {"some_other_key": True}
        assert audit_chain.verify_chain() == {"valid": True, "entry_count": 0}

    @pytest.mark.asyncio
    async def test_multi_node_mini_graph_chains_every_entry_in_order(self):
        """Drives a real LangGraph StateGraph (mirroring test_graph.py's
        mini-graph pattern) through several _with_audit_chain-wrapped nodes,
        the same wiring build_trading_graph() applies to the 8 real nodes.

        This is the test that would have caught runner.py's final_state
        truncation bug had a naive session-end hook been used instead of
        graph-level wrapping: it asserts against verify_chain()'s
        entry_count, not against any single node's return value.
        """
        from langgraph.graph import StateGraph, START, END

        from vesper.graph import _with_audit_chain

        class _MiniState(TypedDict):
            session_id: str
            step: int

        async def node_a(state):
            return {"step": 1, "audit_trail": [{"node": "node_a", "step": 1}]}

        async def node_b(state):
            return {"step": 2, "audit_trail": [{"node": "node_b", "step": 2}]}

        async def node_c(state):
            # Two entries from one node in the same call -- both must chain.
            return {
                "step": 3,
                "audit_trail": [
                    {"node": "node_c", "part": "first"},
                    {"node": "node_c", "part": "second"},
                ],
            }

        workflow = StateGraph(_MiniState)
        workflow.add_node("node_a", _with_audit_chain("node_a", node_a))
        workflow.add_node("node_b", _with_audit_chain("node_b", node_b))
        workflow.add_node("node_c", _with_audit_chain("node_c", node_c))
        workflow.add_edge(START, "node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_edge("node_b", "node_c")
        workflow.add_edge("node_c", END)
        app = workflow.compile()

        await app.ainvoke({"session_id": "sess-mini", "step": 0})

        result = audit_chain.verify_chain()
        assert result == {"valid": True, "entry_count": 4}

        lines = _read_lines()
        assert [l["node"] for l in lines] == ["node_a", "node_b", "node_c", "node_c"]
        assert all(l["session_id"] == "sess-mini" for l in lines)
