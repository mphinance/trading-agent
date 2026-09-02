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
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

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
# M2-01: characterise the CURRENT static-bearer auth path before OAuth lands.
#
# This is the regression net for the rest of M2 — every test below exercises
# `trading_mcp.server._build_auth()` and the MCP_TRANSPORT startup guard as
# they exist TODAY, so a later change that widens what an unauthenticated
# request can reach fails here first. Requests go through a real ASGI
# request/response cycle (`FastMCP.http_app()` + `httpx.ASGITransport`, no
# real socket, no network) rather than calling `verify_token()` directly, so
# these tests would have caught the exact "200 from an unauthenticated
# request" failure mode app_spec.txt §2/M2 calls out by name.
#
# Each test builds its OWN minimal FastMCP app via the real `_build_auth()`
# rather than reusing `trading_mcp.server.mcp` — that module-level singleton
# is constructed once at first import, with whatever TRADING_AGENT_TOKEN was
# (or wasn't) in the environment at the time, so a test-time monkeypatch of
# the env var cannot change its already-built auth provider retroactively.
# Building a fresh app from `_build_auth()` each time tests the actual
# production function against a live-for-the-test token, without paying the
# cost of re-registering all 60 tools per test.
# ═══════════════════════════════════════════════════════════════════════════

_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
_INIT_PAYLOAD = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0"},
    },
}


async def _post_mcp(app, headers: dict[str, str]) -> httpx.Response:
    """POST an `initialize` handshake through a FastMCP ASGI app in-process.

    Runs the app's lifespan (task-group startup) manually, the same thing a
    real ASGI server does before routing a request — without it FastMCP's
    streamable-http session manager raises rather than returning a 4xx/2xx,
    which would make an auth test fail for the wrong reason.
    """
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/mcp", json=_INIT_PAYLOAD, headers=headers)


def test_build_auth_returns_none_when_token_unset(monkeypatch):
    monkeypatch.delenv("TRADING_AGENT_TOKEN", raising=False)
    import trading_mcp.server as srv

    assert srv._build_auth() is None


def test_build_auth_returns_verifier_when_token_set(monkeypatch):
    monkeypatch.setenv("TRADING_AGENT_TOKEN", "m2-01-test-token")
    import trading_mcp.server as srv

    auth = srv._build_auth()
    assert auth is not None
    assert auth.required_scopes == ["read"]


async def test_http_request_with_no_credential_is_rejected(monkeypatch):
    monkeypatch.setenv("TRADING_AGENT_TOKEN", "m2-01-test-token")
    import trading_mcp.server as srv

    app = FastMCP("test-auth", auth=srv._build_auth()).http_app(path="/mcp")
    response = await _post_mcp(app, _MCP_HEADERS)
    assert response.status_code == 401


async def test_http_request_with_wrong_bearer_is_rejected(monkeypatch):
    monkeypatch.setenv("TRADING_AGENT_TOKEN", "m2-01-test-token")
    import trading_mcp.server as srv

    app = FastMCP("test-auth", auth=srv._build_auth()).http_app(path="/mcp")
    headers = {**_MCP_HEADERS, "Authorization": "Bearer not-the-right-token"}
    response = await _post_mcp(app, headers)
    assert response.status_code == 401


async def test_http_request_with_valid_bearer_is_accepted(monkeypatch):
    monkeypatch.setenv("TRADING_AGENT_TOKEN", "m2-01-test-token")
    import trading_mcp.server as srv

    app = FastMCP("test-auth", auth=srv._build_auth()).http_app(path="/mcp")
    headers = {**_MCP_HEADERS, "Authorization": "Bearer m2-01-test-token"}
    response = await _post_mcp(app, headers)
    assert response.status_code == 200


def test_http_transport_refuses_to_start_without_token():
    """Runs `python -m trading_mcp.server` as a real subprocess with
    MCP_TRANSPORT=http and no TRADING_AGENT_TOKEN, and asserts it exits
    non-zero before ever attempting to bind a socket — the SystemExit(1)
    guard at the bottom of trading_mcp/server.py's `__main__` block. This is
    the one piece of auth-adjacent behaviour that lives outside any function
    the test above can call directly, so it's characterised as an actual
    process invocation rather than skipped. No network is touched: the
    process exits during the token check, before `mcp.run_http_async` is
    ever reached.
    """
    env = dict(os.environ)
    env.pop("TRADING_AGENT_TOKEN", None)
    env["MCP_TRANSPORT"] = "http"
    result = subprocess.run(
        [sys.executable, "-m", "trading_mcp.server"],
        env=env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, (
        f"expected a non-zero exit when TRADING_AGENT_TOKEN is unset and "
        f"MCP_TRANSPORT=http, got {result.returncode}. stderr:\n{result.stderr}"
    )
    assert "TRADING_AGENT_TOKEN" in (result.stdout + result.stderr)


# ═══════════════════════════════════════════════════════════════════════════
# M2-02: token comparison is hmac.compare_digest, not a bare dict lookup.
#
# fastmcp's own StaticTokenVerifier checks a presented token with
# `self.tokens.get(token)`, a plain dict lookup that CPython resolves with
# str.__eq__ on a hash collision -- not constant-time. trading_mcp/auth.py's
# HmacStaticTokenVerifier is the drop-in replacement `_build_auth()` now
# constructs; these tests pin both that the helper is actually used (source
# + behavioural spy) and that its accept/reject behaviour is unchanged.
# ═══════════════════════════════════════════════════════════════════════════

def test_build_auth_returns_hmac_verifier_not_stock_one(monkeypatch):
    """`_build_auth()` must build the constant-time verifier, not fastmcp's
    stock `StaticTokenVerifier` -- a regression here would silently revert
    M2-02 while every behavioural test above kept passing (both classes
    accept/reject the same tokens; only the comparison timing differs)."""
    monkeypatch.setenv("TRADING_AGENT_TOKEN", "m2-02-test-token")
    import trading_mcp.server as srv
    from trading_mcp.auth import HmacStaticTokenVerifier

    auth = srv._build_auth()
    assert isinstance(auth, HmacStaticTokenVerifier)


def test_hmac_static_token_verifier_source_uses_compare_digest():
    """Source-level pin, mirroring test_notify.py's grep-shaped assertions:
    fails the moment verify_token stops routing through
    hmac.compare_digest, not just when today's behaviour looks right."""
    import inspect

    from trading_mcp.auth import HmacStaticTokenVerifier

    source = inspect.getsource(HmacStaticTokenVerifier.verify_token)
    assert "hmac.compare_digest" in source
    assert ".get(token)" not in source, "must not fall back to a bare dict lookup"


async def test_verify_token_actually_invokes_compare_digest(monkeypatch):
    """Behavioural spy: verify_token must call hmac.compare_digest at least
    once per verification, not just import the module and never use it."""
    import trading_mcp.auth as auth_mod

    calls = []
    real_compare_digest = auth_mod.hmac.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", _spy)

    verifier = auth_mod.HmacStaticTokenVerifier(
        tokens={"good-token": {"client_id": "owner", "scopes": ["read"]}},
        required_scopes=["read"],
    )

    accepted = await verifier.verify_token("good-token")
    assert accepted is not None
    assert accepted.client_id == "owner"
    assert calls, "verify_token accepted a token without calling hmac.compare_digest"

    calls.clear()
    rejected = await verifier.verify_token("wrong-token")
    assert rejected is None
    assert calls, "verify_token rejected a token without calling hmac.compare_digest"


async def test_hmac_verifier_rejects_missing_required_scope():
    import trading_mcp.auth as auth_mod

    verifier = auth_mod.HmacStaticTokenVerifier(
        tokens={"scoped-token": {"client_id": "owner", "scopes": []}},
        required_scopes=["read"],
    )
    assert await verifier.verify_token("scoped-token") is None


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
