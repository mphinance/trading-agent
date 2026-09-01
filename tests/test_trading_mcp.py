"""Hermetic tests for trading_mcp/ -- the owner-only, read-only MCP server.

Two things this file exists to pin down, beyond ordinary correctness:

1. **The tool set actually assembles.** `trading_mcp/server.py` and
   `trading_mcp/vesper_tools.py` are written by different hands against a
   documented contract (`register_vesper_tools(mcp) -> list[str]`); a
   signature drift or a name collision with one of `mcp_server/registry.py`'s
   47 momentum tools would only show up at process start otherwise.
2. **Rule 3 stays mechanically enforced.** "Any adapter that grows its own
   order path is a new threat model, not a small addition" (CLAUDE.md) is
   easy to honor today and easy to erode one convenience wrapper at a time.
   `test_execution_guard_order_path_confined_to_known_call_sites` and its
   two narrower siblings are this file's answer to that -- modeled on
   `test_notify.py`'s ntfy-topic-never-in-status() assertion: a grep-shaped
   check that fails the moment it stops being true, not a comment asking
   nicely.

Every vesper tool wraps an already-tested function and is supposed to
degrade to `{"available": False, "reason": ...}` (or an equivalent shape)
rather than raise. The degrade tests below break each tool's ONE reused
dependency (via monkeypatch, never by poisoning shared global state that
outlives the test) and assert the wrapper survives it.
"""

from __future__ import annotations

import os
import ast
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _boom(*_a, **_kw):
    raise RuntimeError("simulated failure")


async def _aboom(*_a, **_kw):
    raise RuntimeError("simulated failure")


class _CollectingMCP:
    """Stand-in for a FastMCP instance that just captures every
    `@mcp.tool()`-decorated function under its own name, so a tool can be
    invoked directly in a test without spinning up a real MCP transport or
    its pydantic validation layer -- the same "stub FastMCP double" the
    vesper_tools.py build report describes using for manual verification.

    Every call site in vesper_tools.py uses the bare `@mcp.tool()` form (no
    args), which is all this needs to support.
    """

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *_args, **_kwargs):
        def _decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return _decorator


@pytest.fixture
def vtools():
    """The 13 Vesper read-only tools, registered fresh onto a collecting
    stand-in and returned as {name: callable}."""
    from trading_mcp.vesper_tools import register_vesper_tools

    mcp = _CollectingMCP()
    names = register_vesper_tools(mcp)
    assert set(names) == set(mcp.tools), "register_vesper_tools's returned names must match what it actually registered"
    return mcp.tools


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end assembly: the two halves actually fit together
# ═══════════════════════════════════════════════════════════════════════════

async def test_server_registers_expected_tool_count():
    """47 momentum tools (tiers 1-3) + the 13 Vesper read-only tools, with no
    signature mismatch or import error between the two registration passes."""
    import trading_mcp.server as srv

    tools = await srv.mcp.list_tools()
    names = [t.name for t in tools]
    assert len(names) == 60, f"expected 47 momentum + 13 vesper = 60 tools, got {len(names)}: {sorted(names)}"


async def test_no_duplicate_tool_names_between_momentum_and_vesper():
    """A duplicate name here means one registration pass silently shadowed
    the other -- FastMCP wouldn't refuse it, it would just make one tool
    unreachable."""
    import trading_mcp.server as srv

    tools = await srv.mcp.list_tools()
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), f"duplicate tool names: {sorted(n for n in set(names) if names.count(n) > 1)}"


def test_vesper_tools_registers_exactly_the_documented_thirteen(vtools):
    expected = {
        "get_account_state", "get_halt_status", "get_drawdown_status",
        "get_paper_positions", "get_paper_summary", "list_alerts",
        "list_pending_proposals", "get_proposal", "get_audit_trail",
        "verify_audit_chain", "get_playbook_calibration",
        "recall_similar_setups", "get_position_monitor_status",
    }
    assert set(vtools) == expected


def test_server_construction_makes_no_network_or_broker_call():
    """Importing trading_mcp.server must be side-effect-free beyond wiring
    tool closures -- no broker client is constructed, no HTTP call is made,
    at import time. If this test hangs or raises against a machine with no
    network and no Webull credentials, something now reaches out at import."""
    import trading_mcp.server as srv

    # Asserted against an explicitly-cleared env rather than trusting the
    # ambient shell -- otherwise this passes for the wrong reason on a machine
    # that happens not to export TRADING_AGENT_TOKEN.
    assert os.environ.get("TRADING_AGENT_TOKEN") is None
    assert srv.mcp.auth is None


# ═══════════════════════════════════════════════════════════════════════════
# Rule 3 pin: the order path stays in exactly one place
# ═══════════════════════════════════════════════════════════════════════════

def _guard_names(tree: ast.AST) -> set[str]:
    """Local names bound to `vesper.execution_guard`'s `guard` singleton.

    Always includes the literal name "guard": a false positive costs nothing
    (don't name an unrelated object `guard`), a false negative is a missed
    order path, so this errs toward flagging.
    """
    names = {"guard"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("execution_guard"):
            for alias in node.names:
                if alias.name == "guard":
                    names.add(alias.asname or alias.name)
    return names


def _guard_call_sites(pyfile: Path) -> list[str]:
    """Return ['preview', 'place', ...] for every reference to the execution
    guard's order methods in `pyfile`.

    Matches ATTRIBUTE ACCESS, not merely invocation, because this codebase's
    own order path passes the bound method rather than calling it inline --
    `asyncio.to_thread(guard.place, ticket_id, payload, fn)` at
    executor.py:262 and monitor.py:404. A Call-only check misses exactly the
    idiom a new order path is most likely to copy from the existing one, and
    an earlier version of this test did. Import aliases resolve too, so
    `from vesper.execution_guard import guard as g; g.place(...)` is caught.

    Deliberately AST-based, not a text grep: this module's and
    vesper_tools.py's docstrings describe the rule using the literal
    substrings "guard.preview(" / "guard.place(" as prose, which a text
    search would wrongly flag as violations.
    """
    try:
        tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
    except (SyntaxError, UnicodeDecodeError):
        return []
    names = _guard_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in ("preview", "place"):
            continue
        base = node.value
        if isinstance(base, ast.Name) and base.id in names:
            found.append(node.attr)          # guard.place / g.place
        elif isinstance(base, ast.Attribute) and base.attr == "guard":
            found.append(node.attr)          # vesper.execution_guard.guard.place
    return found


def test_guard_pin_catches_indirect_call_shapes(tmp_path):
    """The pin must catch the ways an order path could hide, not just the
    obvious one. Each snippet below is a real bypass of the Call-only version
    this test replaced -- (b) in particular is the exact idiom the live order
    path already uses, so a new tool copying it would have gone undetected.
    """
    bypasses = {
        "direct": "from vesper.execution_guard import guard\nguard.place(1, {}, None)\n",
        "renamed_import": "from vesper.execution_guard import guard as g\ng.place(1, {}, None)\n",
        "bound_method": (
            "import asyncio\n"
            "from vesper.execution_guard import guard\n"
            "asyncio.to_thread(guard.place, 1, {}, None)\n"
        ),
        "dotted_module": "import vesper.execution_guard\nvesper.execution_guard.guard.place(1, {}, None)\n",
    }
    for label, src in bypasses.items():
        f = tmp_path / f"{label}.py"
        f.write_text(src, encoding="utf-8")
        assert _guard_call_sites(f), f"pin failed to catch bypass shape: {label}"

    # ...and prose describing the rule is still not a violation.
    clean = tmp_path / "prose.py"
    clean.write_text('"""No tool here may call guard.place( or guard.preview(."""\n', encoding="utf-8")
    assert _guard_call_sites(clean) == []


def test_trading_mcp_never_calls_execution_guard():
    """Rule 3: this package reads state, it never moves money."""
    offenders = {
        str(p): _guard_call_sites(p)
        for p in (REPO_ROOT / "trading_mcp").rglob("*.py")
        if _guard_call_sites(p)
    }
    assert offenders == {}


def test_mcp_server_never_calls_execution_guard():
    """Same pin for the pre-existing stdio momentum server -- its
    no-broker-credentials property is load-bearing per CLAUDE.md and this
    phase must not be the thing that erodes it."""
    offenders = {
        str(p): _guard_call_sites(p)
        for p in (REPO_ROOT / "mcp_server").rglob("*.py")
        if _guard_call_sites(p)
    }
    assert offenders == {}


def test_execution_guard_order_path_confined_to_known_call_sites():
    """Across the whole non-test source tree, `guard.preview()` /
    `guard.place()` may only be called from the two places CLAUDE.md's rule 3
    and Status section already document as reaching the broker:
    `vesper/nodes/executor.py` (the approved order path, gated by a
    Telegram/Discord tap resuming the checkpointed graph) and
    `vesper/monitor.py` (the position monitor's own exit cascade, a
    pre-existing and separately-tested auto-exit path -- not something this
    phase adds).

    Anything else picking up a call here is a NEW order path, and rule 3 is
    explicit that that's "a new threat model, not a small addition."
    """
    known_call_sites = {
        REPO_ROOT / "vesper" / "nodes" / "executor.py",
        REPO_ROOT / "vesper" / "monitor.py",
    }
    offenders = []
    for pyfile in REPO_ROOT.rglob("*.py"):
        parts = pyfile.parts
        if ".venv" in parts or "__pycache__" in parts or "tests" in parts:
            continue
        if pyfile in known_call_sites:
            continue
        if _guard_call_sites(pyfile):
            offenders.append(str(pyfile))
    assert offenders == []

    # And the known call sites still actually call it -- this is the other
    # half of the pin: a refactor that quietly DELETES the order path (e.g.
    # executor.py starts placing orders some other way) should also fail here.
    for pyfile in known_call_sites:
        assert _guard_call_sites(pyfile), f"expected {pyfile} to call guard.preview()/guard.place()"


# ═══════════════════════════════════════════════════════════════════════════
# Per-tool graceful degradation
# ═══════════════════════════════════════════════════════════════════════════

async def test_get_account_state_degrades_when_webull_unconfigured(vtools, monkeypatch):
    import wb

    class _BrokenWebull:
        def __init__(self):
            raise wb.WebullError("Webull credentials not configured")

    monkeypatch.setattr(wb, "Webull", _BrokenWebull)
    result = await vtools["get_account_state"]()
    assert result["available"] is False
    assert "reason" in result


def test_get_halt_status_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("vesper.halt.get_halt_status", _boom)
    result = vtools["get_halt_status"]()
    assert result == {"available": False, "reason": "simulated failure"}


def test_get_drawdown_status_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("vesper.circuit_breaker.get_peak_nlv", _boom)
    result = vtools["get_drawdown_status"]()
    assert result["available"] is False


def test_get_paper_positions_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("vesper.paper_ledger.get_paper_positions", _boom)
    result = vtools["get_paper_positions"]()
    assert result["available"] is False


def test_get_paper_summary_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("vesper.paper_ledger.get_paper_summary", _boom)
    result = vtools["get_paper_summary"]()
    assert result["available"] is False


def test_list_alerts_degrades_when_store_unreadable(vtools, monkeypatch):
    class _BrokenStore:
        def __init__(self, *_a, **_kw):
            raise OSError("disk full")

    monkeypatch.setattr("alerts.AlertStore", _BrokenStore)
    result = vtools["list_alerts"]()
    assert result["available"] is False


def test_list_alerts_reports_unavailable_when_tdpro_down(vtools, monkeypatch):
    """Rule 4c, mechanically: a dynamic alert whose level can't be resolved
    right now must surface as unavailable, never as a stale/remembered
    number. Uses the real `alerts.resolve_level()` -- only the alert store
    and the TDPro-backed `levels_of()` are faked -- so this exercises the
    actual crossing/resolution code path `list_alerts` runs in production,
    not a re-implementation of it."""
    dynamic_alert = {
        "id": "a1", "symbol": "SPY", "direction": "above",
        "level_ref": "flip", "level_static": None, "state": "pending",
        "note": "", "last_price": 600.0, "repeat": False, "trigger_count": 0,
    }
    static_alert = {
        "id": "a2", "symbol": "AAPL", "direction": "below",
        "level_ref": None, "level_static": 150.0, "state": "pending",
        "note": "", "last_price": 155.0, "repeat": False, "trigger_count": 0,
    }

    class _FakeStore:
        def __init__(self, *_a, **_kw):
            pass

        def list(self):
            return [dynamic_alert, static_alert]

    monkeypatch.setattr("alerts.AlertStore", _FakeStore)
    # Simulates TDPro being unreachable: _build_levels_of()'s own docstring
    # says levels_of() returns None (never a remembered number) in that case.
    monkeypatch.setattr("vesper.alerts_runner._build_levels_of", lambda: (lambda symbol: None))

    result = vtools["list_alerts"]()
    assert result["available"] is True
    by_id = {a["id"]: a for a in result["alerts"]}

    dynamic_out = by_id["a1"]
    assert dynamic_out["current_level"] is None
    assert dynamic_out["level_unavailable"] is True

    # A static alert has no TDPro dependency at all -- it must be unaffected
    # by TDPro being down.
    static_out = by_id["a2"]
    assert static_out["current_level"] == 150.0
    assert static_out["level_unavailable"] is False


def test_list_pending_proposals_degrades_on_error(vtools, monkeypatch):
    from vesper.bot.inbound import approval_registry

    monkeypatch.setattr(approval_registry, "list_pending", _boom)
    result = vtools["list_pending_proposals"]()
    assert result["available"] is False


def test_get_proposal_degrades_on_error(vtools, monkeypatch):
    from vesper.bot.inbound import approval_registry

    monkeypatch.setattr(approval_registry, "get_pending", _boom)
    result = vtools["get_proposal"]("prop-123")
    assert result["available"] is False


def test_get_audit_trail_degrades_when_chain_path_unreadable(vtools, monkeypatch, tmp_path):
    from vesper import audit_chain

    # A directory sitting where a file is expected: Path.exists() is True
    # (so the tool doesn't take the empty-chain early return), but
    # open(path, "rb") raises IsADirectoryError -- simulating "the chain
    # path exists but can't be read" without corrupting anything real.
    bogus_chain_path = tmp_path / "audit_chain.jsonl"
    bogus_chain_path.mkdir()
    monkeypatch.setattr(audit_chain, "_CHAIN_PATH", bogus_chain_path)

    result = vtools["get_audit_trail"]()
    assert result["available"] is False


def test_verify_audit_chain_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("vesper.audit_chain.verify_chain", _boom)
    result = vtools["verify_audit_chain"]()
    assert result["available"] is False


def test_get_playbook_calibration_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("mcp_server.conviction.get_playbook_performance", _boom)
    result = vtools["get_playbook_calibration"]("wheel", days=30)
    assert result["available"] is False


async def test_recall_similar_setups_degrades_without_chromadb(vtools, monkeypatch):
    """Simulates a chromadb-less environment: `mcp_server/knowledge.py`
    imports `chromadb` at module level (an optional, sometimes-heavy dep --
    see vesper_tools.py's own docstring), so an environment without it would
    fail to import that whole module. Setting the module to None in
    sys.modules is the standard way to force that ImportError without
    actually uninstalling chromadb from this test environment, where it IS
    present."""
    monkeypatch.setitem(sys.modules, "mcp_server.knowledge", None)
    result = await vtools["recall_similar_setups"](query_thesis="oversold bounce off the 200dma")
    assert result["available"] is False
    assert "trade memory" in result["reason"]


async def test_get_position_monitor_status_degrades_on_poll_failure(vtools, monkeypatch):
    from vesper.monitor import PositionMonitor

    monkeypatch.setattr(PositionMonitor, "poll_webull_positions", _aboom)
    result = await vtools["get_position_monitor_status"]()
    assert result["available"] is False


# ═══════════════════════════════════════════════════════════════════════════
# No tool here can act -- only read
# ═══════════════════════════════════════════════════════════════════════════

def test_no_vesper_tool_can_reach_halt_or_resume():
    """halt() and resume() (the MUTATING pair in vesper/halt.py) and
    ApprovalRegistry.submit_decision() must never be imported by this
    module -- only their read counterparts (get_halt_status,
    list_pending/get_pending/get_decision)."""
    import trading_mcp.vesper_tools as module

    src = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported_names = set()
    for node in ast.walk(src):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)

    forbidden = {"halt", "resume", "submit_decision"}
    assert not (imported_names & forbidden), f"forbidden mutating import(s) found: {imported_names & forbidden}"
