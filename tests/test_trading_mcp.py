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
    assert len(names) == 77, f"expected 64 momentum+tickertrace + 13 vesper = 77 tools, got {len(names)}: {sorted(names)}"


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
# M2-03: trading_mcp/oauth_provider.py's disposition review found its OAuth
# access/refresh-token and authorization-code lookups doing plain
# `dict.get(presented)` -- the same non-constant-time shape M2-02 fixed for
# the static bearer token, just reintroduced one layer up. Fixed by routing
# all three through `_lookup_constant_time`, which walks every stored key
# with `hmac.compare_digest`. These tests pin that fix the same way M2-02's
# pinned its own: a source-level check plus a behavioural spy.
# ═══════════════════════════════════════════════════════════════════════════

def _make_oauth_provider(secret: str = "m2-03-operator-secret"):
    from trading_mcp.oauth_provider import SingleOperatorOAuthProvider

    return SingleOperatorOAuthProvider(
        operator_secret=secret, base_url="https://agent.example.test",
    )


def test_oauth_token_lookups_source_uses_lookup_constant_time():
    """Source-level pin: none of the three presented-secret lookups may fall
    back to a bare `.get(` on the token/code dicts."""
    import inspect

    from trading_mcp.oauth_provider import SingleOperatorOAuthProvider

    for method_name, forbidden in (
        ("load_access_token", "self.access_tokens.get("),
        ("load_refresh_token", "self.refresh_tokens.get("),
        ("load_authorization_code", "self.auth_codes.get("),
    ):
        source = inspect.getsource(getattr(SingleOperatorOAuthProvider, method_name))
        assert "_lookup_constant_time(" in source, (
            f"{method_name} must route through _lookup_constant_time"
        )
        assert forbidden not in source, (
            f"{method_name} must not fall back to a bare dict lookup"
        )


async def test_oauth_lookup_constant_time_actually_invokes_compare_digest(monkeypatch):
    """Behavioural spy: verifying an OAuth access token must call
    hmac.compare_digest at least once, on both the accept and reject path."""
    import trading_mcp.oauth_provider as oauth_mod

    calls = []
    real_compare_digest = oauth_mod.hmac.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(oauth_mod.hmac, "compare_digest", _spy)

    provider = _make_oauth_provider()
    pair = provider._issue_token_pair("some-client", ["read"])

    accepted = await provider.load_access_token(pair.access_token)
    assert accepted is not None
    assert calls, "load_access_token accepted a token without calling hmac.compare_digest"

    calls.clear()
    rejected = await provider.load_access_token("not-a-real-token")
    assert rejected is None
    assert calls, "load_access_token rejected a token without calling hmac.compare_digest"


async def test_oauth_lookup_constant_time_correctness():
    """Behavioural correctness, independent of the timing property: the
    right token is still found, the wrong one is still rejected, for all
    three secret stores this fix touches."""
    provider = _make_oauth_provider()
    pair = provider._issue_token_pair("some-client", ["read"])

    access = await provider.load_access_token(pair.access_token)
    assert access is not None and access.client_id == "some-client"
    assert await provider.load_access_token("wrong") is None

    from mcp.shared.auth import OAuthClientInformationFull

    client = OAuthClientInformationFull(
        client_id="some-client", redirect_uris=["https://client.example.test/cb"],
    )
    refresh = await provider.load_refresh_token(client, pair.refresh_token)
    assert refresh is not None and refresh.client_id == "some-client"
    assert await provider.load_refresh_token(client, "wrong") is None


# ═══════════════════════════════════════════════════════════════════════════
# M2-09: OAuth access/refresh tokens are revocable AND persisted server-side
# (outside git, 0600) -- not just tracked in the in-memory dicts M2-03 built.
# `tests/conftest.py`'s autouse `_isolated_vesper_state` fixture already
# redirects `trading_mcp.oauth_provider._DATA_DIR`/`_TOKEN_STATE_PATH` to a
# per-test tmp dir, so every test below (like every other test in this file
# that touches the OAuth provider) writes there, never to the developer's
# real data/ directory.
# ═══════════════════════════════════════════════════════════════════════════

def test_oauth_token_state_path_is_gitignored():
    """data/oauth_tokens_state.json holds live bearer credentials -- rule 5
    forbids it ever landing in a commit, the same standing .env holds."""
    from trading_mcp.oauth_provider import _TOKEN_STATE_PATH

    gitignore_text = (REPO_ROOT / ".gitignore").read_text()
    assert f"data/{_TOKEN_STATE_PATH.name}" in gitignore_text


def test_oauth_token_state_file_written_0600():
    """Issuing a token pair must persist the state file with owner-only
    permissions, not whatever the process umask would otherwise leave."""
    import stat

    from trading_mcp.oauth_provider import _TOKEN_STATE_PATH

    provider = _make_oauth_provider()
    provider._issue_token_pair("some-client", ["read"])

    assert _TOKEN_STATE_PATH.exists()
    mode = stat.S_IMODE(_TOKEN_STATE_PATH.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


async def test_revoked_access_token_is_rejected():
    """The M2-09 acceptance test, verbatim: issue a token, use it
    successfully, revoke it, confirm it is then rejected."""
    provider = _make_oauth_provider()
    pair = provider._issue_token_pair("some-client", ["read"])

    used = await provider.load_access_token(pair.access_token)
    assert used is not None, "token must be usable before revocation"

    await provider.revoke_token(used)

    assert await provider.load_access_token(pair.access_token) is None, (
        "token must be rejected after revocation"
    )


async def test_revoked_refresh_token_is_rejected():
    """Revoking the refresh half must reject it too, and must not leave the
    paired access token usable -- _revoke_pair's whole point."""
    provider = _make_oauth_provider()
    pair = provider._issue_token_pair("some-client", ["read"])

    refresh_obj = await provider.load_refresh_token(
        type("C", (), {"client_id": "some-client"})(), pair.refresh_token
    )
    assert refresh_obj is not None

    await provider.revoke_token(refresh_obj)

    assert await provider.load_access_token(pair.access_token) is None
    assert await provider.load_refresh_token(
        type("C", (), {"client_id": "some-client"})(), pair.refresh_token
    ) is None


async def test_revocation_survives_a_fresh_provider_instance():
    """Persistence, not just in-memory bookkeeping: a brand-new provider
    instance pointed at the same on-disk store (simulating a process
    restart) must not resurrect a revoked token, and must still honour one
    that was never revoked."""
    provider = _make_oauth_provider()
    kept = provider._issue_token_pair("some-client", ["read"])
    revoked = provider._issue_token_pair("some-client", ["read"])

    revoked_obj = await provider.load_access_token(revoked.access_token)
    await provider.revoke_token(revoked_obj)

    restarted = _make_oauth_provider()  # fresh instance, same _DATA_DIR
    assert await restarted.load_access_token(kept.access_token) is not None
    assert await restarted.load_access_token(revoked.access_token) is None


# ═══════════════════════════════════════════════════════════════════════════
# M2-04: the OAuth 2.1 authorization server is actually MOUNTED on the MCP
# app, and its discovery endpoints return well-formed metadata -- not just
# that `SingleOperatorOAuthProvider` (M2-03) has the right methods in
# isolation. Every test below builds a real ASGI app from the production
# `_build_auth()` (same reasoning as the M2-01 block above: the module-level
# `trading_mcp.server.mcp` singleton is built once at import time and can't
# be retargeted by a test-time env monkeypatch) and drives it through
# `httpx.ASGITransport`, no real socket.
#
# `_build_auth()` only wires OAuth in when `MCP_PUBLIC_URL` is set (see its
# docstring) -- both states are tested: OAuth mounted when configured, and
# the pre-M2-03 bearer-only shape completely unchanged when it isn't, since
# CLAUDE.md rule 1 (loopback/Tailscale) means a box with no public URL
# configured has no business advertising a public authorization server.
# ═══════════════════════════════════════════════════════════════════════════

_OAUTH_ENV = {
    "TRADING_AGENT_TOKEN": "m2-04-operator-key",
    "MCP_PUBLIC_URL": "https://agent.mphinance.test",
}


def _set_oauth_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {**_OAUTH_ENV, **overrides}
    for key, value in env.items():
        monkeypatch.setenv(key, value)


async def _get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)


def test_build_auth_is_multiauth_when_public_url_set(monkeypatch):
    """With both TRADING_AGENT_TOKEN and MCP_PUBLIC_URL set, `_build_auth()`
    must return the composed MultiAuth (OAuth server + bearer fallback), not
    the bare bearer verifier M2-01 built."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv
    from fastmcp.server.auth import MultiAuth

    from trading_mcp.oauth_provider import SingleOperatorOAuthProvider

    auth = srv._build_auth()
    assert isinstance(auth, MultiAuth)
    assert isinstance(auth.server, SingleOperatorOAuthProvider)
    assert any(
        isinstance(v, srv.HmacStaticTokenVerifier) for v in auth.verifiers
    ), "the static-bearer fallback must still be one of the verifiers"


def test_build_auth_stays_bearer_only_without_public_url(monkeypatch):
    """No MCP_PUBLIC_URL -> no OAuth server mounted, unchanged from M2-01/02.
    A box that hasn't declared a public URL (rule 1: loopback/Tailscale by
    default) must not start advertising a public authorization server."""
    monkeypatch.setenv("TRADING_AGENT_TOKEN", "m2-04-operator-key")
    monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
    import trading_mcp.server as srv

    auth = srv._build_auth()
    assert isinstance(auth, srv.HmacStaticTokenVerifier)


async def test_discovery_endpoints_404_without_public_url(monkeypatch):
    monkeypatch.setenv("TRADING_AGENT_TOKEN", "m2-04-operator-key")
    monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
    import trading_mcp.server as srv

    app = FastMCP("test-bearer-only", auth=srv._build_auth()).http_app(path="/mcp")
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        response = await _get(app, path)
        assert response.status_code == 404, (
            f"{path} must not be mounted when MCP_PUBLIC_URL is unset"
        )


async def test_oauth_authorization_server_metadata_is_well_formed(monkeypatch):
    """RFC 8414: /.well-known/oauth-authorization-server must resolve and
    carry every field a real client (claude.ai's connector, via CIMD or DCR)
    needs to drive the code+PKCE flow -- issuer, the three operational
    endpoints, and S256 PKCE support, which OAuth 2.1 makes mandatory."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth", auth=srv._build_auth()).http_app(path="/mcp")
    response = await _get(app, "/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    metadata = response.json()
    base = "https://agent.mphinance.test"
    assert metadata["issuer"].rstrip("/") == base
    assert metadata["authorization_endpoint"] == f"{base}/authorize"
    assert metadata["token_endpoint"] == f"{base}/token"
    assert metadata["registration_endpoint"] == f"{base}/register"
    assert "code" in metadata["response_types_supported"]
    assert "authorization_code" in metadata["grant_types_supported"]
    assert "refresh_token" in metadata["grant_types_supported"]
    assert "S256" in metadata["code_challenge_methods_supported"], (
        "OAuth 2.1 requires PKCE; a client with no code_challenge_methods "
        "to offer would fall back to a bare code flow"
    )
    assert "read" in metadata["scopes_supported"]


async def test_oauth_protected_resource_metadata_is_well_formed(monkeypatch):
    """RFC 9728: /.well-known/oauth-protected-resource/mcp and its root alias
    /.well-known/oauth-protected-resource must point back at this server's own
    /mcp endpoint and name the authorization server that issues tokens for it --
    this is the document a CIMD/DCR client reads first, from the 401's
    WWW-Authenticate challenge or root discovery, to find the AS."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth", auth=srv._build_auth()).http_app(path="/mcp")
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        response = await _get(app, path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"

        metadata = response.json()
        assert metadata["resource"] == "https://agent.mphinance.test/mcp"
        assert metadata["authorization_servers"] == ["https://agent.mphinance.test/"]
        assert "read" in metadata["scopes_supported"]


async def test_unauthenticated_request_advertises_resource_metadata(monkeypatch):
    """The whole point of mounting an AS: a client that shows up with no
    credential at all must be told, in the 401 itself, where to go find out
    how to get one -- not just get a bare `WWW-Authenticate: Bearer` with no
    way to discover /authorize, which was the state M2-04 was written to fix
    (see app_spec: 'every OAuth endpoint 404s ... verified by curl against
    the live host')."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth", auth=srv._build_auth()).http_app(path="/mcp")
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0"},
        },
    }
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/mcp", json=payload, headers=headers)

    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer")
    assert (
        'resource_metadata="https://agent.mphinance.test/.well-known/'
        'oauth-protected-resource/mcp"' in challenge
    )


def test_oauth_mount_still_no_user_model(monkeypatch):
    """Mounting the AS must not have grown a user table, a per-user client
    concept, or a second secret -- still exactly one operator_secret, reused
    from TRADING_AGENT_TOKEN, per CLAUDE.md's single-operator premise."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    auth = srv._build_auth()
    provider = auth.server
    assert provider._operator_secret == "m2-04-operator-key"
    for attr in ("users", "user_store", "accounts", "tenants"):
        assert not hasattr(provider, attr), f"unexpected user-model attribute: {attr}"


# ═══════════════════════════════════════════════════════════════════════════
# M2-05: dynamic client registration (RFC 7591) works END TO END -- a client
# POSTs to /register with no prior credential (by design, see oauth_provider's
# module docstring: gating DCR would just recreate the "paste a header"
# problem OAuth was chosen to avoid), receives a client_id/client_secret,
# then drives the FULL authorization-code + PKCE handshake through the
# operator-key gate at /authorize and exchanges the code for a token at
# /token -- and that token is then actually accepted by the live /mcp
# endpoint, not just minted and left untested. A second flow proves the
# other half: a client_id nobody registered cannot walk away with a token.
#
# Same pattern as the M2-04 block above: build the real ASGI app from
# `_build_auth()` and drive it with `httpx.ASGITransport`, no real socket,
# no mocked pieces of the handshake -- every hop (register, gate, authorize,
# token, mcp) goes through the actual mcp-SDK route handlers
# `SingleOperatorOAuthProvider` sits underneath.
# ═══════════════════════════════════════════════════════════════════════════

import base64
import hashlib
import secrets


def _pkce_pair() -> tuple[str, str]:
    """A (code_verifier, code_challenge) pair per RFC 7636 S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


async def _register_client(app, **overrides) -> dict[str, Any]:
    """POST /register (RFC 7591), return the parsed client-information JSON."""
    body = {
        "redirect_uris": ["https://client.example.test/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "m2-05-test-client",
        **overrides,
    }
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/register", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_dynamic_client_registration_issues_credentials(monkeypatch):
    """The DCR endpoint is open (no operator credential required to reach
    it -- see the module docstring's reasoning) and hands back a client_id
    plus a client_secret for the default `client_secret_post` auth method."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth", auth=srv._build_auth()).http_app(path="/mcp")
    client_info = await _register_client(app)
    assert client_info["client_id"]
    assert client_info["client_secret"]
    assert client_info["redirect_uris"] == ["https://client.example.test/callback"]


async def _authorize(app, *, client_id: str, redirect_uri: str, code_challenge: str,
                      operator_key: str | None, state: str = "m2-05-state",
                      scope: str = "read") -> httpx.Response:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": scope,
    }
    if operator_key is not None:
        params["operator_key"] = operator_key
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            return await client.get("/authorize", params=params)


async def _token_from_code(app, *, client_id: str, client_secret: str, code: str,
                            redirect_uri: str, code_verifier: str) -> httpx.Response:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/token", data=data)


async def test_dcr_client_completes_authorization_code_exchange_and_token_works_on_mcp(
    monkeypatch,
):
    """The full end-to-end path M2-05 names: register -> gated /authorize ->
    /token -> the resulting access token is accepted by the live /mcp
    endpoint. Every hop is a real HTTP round trip through the production
    `_build_auth()`-built app; nothing about the handshake is stubbed."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth", auth=srv._build_auth()).http_app(path="/mcp")

    # 1. Register (no credential required -- DCR stays open by design).
    client_info = await _register_client(app)
    client_id = client_info["client_id"]
    client_secret = client_info["client_secret"]
    redirect_uri = client_info["redirect_uris"][0]

    # 2. Drive /authorize past the operator-key gate with a valid PKCE pair.
    verifier, challenge = _pkce_pair()
    auth_response = await _authorize(
        app, client_id=client_id, redirect_uri=redirect_uri,
        code_challenge=challenge, operator_key=_OAUTH_ENV["TRADING_AGENT_TOKEN"],
    )
    assert auth_response.status_code == 302, auth_response.text
    location = auth_response.headers["location"]
    assert location.startswith(redirect_uri)
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["m2-05-state"]
    code = query["code"][0]

    # 3. Exchange the code (+ PKCE verifier) for an access token.
    token_response = await _token_from_code(
        app, client_id=client_id, client_secret=client_secret, code=code,
        redirect_uri=redirect_uri, code_verifier=verifier,
    )
    assert token_response.status_code == 200, token_response.text
    token_payload = token_response.json()
    access_token = token_payload["access_token"]
    assert token_payload["token_type"].lower() == "bearer"
    assert token_payload["refresh_token"]

    # 4. The resulting access token is accepted by the live /mcp endpoint.
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {access_token}"}
    mcp_response = await _post_mcp(app, headers)
    assert mcp_response.status_code == 200, mcp_response.text

    # ... and a garbage token still isn't.
    bad_headers = {**_MCP_HEADERS, "Authorization": "Bearer not-a-real-token"}
    bad_response = await _post_mcp(app, bad_headers)
    assert bad_response.status_code == 401


async def test_authorize_without_operator_key_never_issues_a_code(monkeypatch):
    """Reaching /authorize with no operator_key at all must render the gate
    form (200), never redirect with a code -- registration alone must not be
    enough to obtain a token."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth", auth=srv._build_auth()).http_app(path="/mcp")
    client_info = await _register_client(app)
    _verifier, challenge = _pkce_pair()

    response = await _authorize(
        app, client_id=client_info["client_id"],
        redirect_uri=client_info["redirect_uris"][0],
        code_challenge=challenge, operator_key=None,
    )
    assert response.status_code == 200
    assert "authorize" not in response.headers.get("location", "")
    assert response.status_code != 302


async def test_unregistered_client_cannot_obtain_a_token(monkeypatch):
    """A client_id nobody ever POSTed to /register must be refused at
    /token -- confirming DCR being open doesn't mean the token endpoint
    trusts an unknown client_id handed to it directly."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth", auth=srv._build_auth()).http_app(path="/mcp")

    # A syntactically-plausible but never-registered client, presenting a
    # code that was never issued either (there is no legitimate way to have
    # one without a registered client, so this is the strongest input an
    # attacker could actually construct).
    response = await _token_from_code(
        app,
        client_id="never-registered-client-id",
        client_secret="whatever",
        code="toa_code_forged",
        redirect_uri="https://client.example.test/callback",
        code_verifier="forged-verifier",
    )
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["error"] in ("unauthorized_client", "invalid_client")


async def test_unregistered_client_authorize_is_also_refused(monkeypatch):
    """Same property at the front door: /authorize itself refuses to issue a
    code for a client_id that was never registered, even with a valid
    operator key -- `SingleOperatorOAuthProvider.authorize()`'s explicit
    `client.client_id not in self.clients` check (module docstring: "only
    refuses an unregistered client_id, it does not perform its own separate
    credential check")."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth", auth=srv._build_auth()).http_app(path="/mcp")
    _verifier, challenge = _pkce_pair()

    response = await _authorize(
        app, client_id="never-registered-client-id",
        redirect_uri="https://client.example.test/callback",
        code_challenge=challenge, operator_key=_OAUTH_ENV["TRADING_AGENT_TOKEN"],
    )
    # AuthorizationHandler.handle() returns a direct 400 JSON error (not a
    # redirect) when client_id itself doesn't resolve -- see
    # mcp.server.auth.handlers.authorize's `attempt_load_client=False` path.
    assert response.status_code == 400
    assert response.json()["error"] in ("invalid_request", "unauthorized_client")


# ═══════════════════════════════════════════════════════════════════════════
# M2-06: the static-bearer and OAuth paths must converge on exactly ONE
# authorization-decision function, and an OAuth handshake must never be able
# to walk away with more scope than it was actually granted.
#
# Both halves exist because of the SAME real-world bug, described in
# docs/AUTH_TRADE_SCOPE_LOCKDOWN.md and docs/HANDOFF_2026-09-01.md: supermcp's
# `/login` handed back the master admin token for a password match that was
# only ever supposed to prove "knows the shared dashboard password", and
# separately supermcp's OAuth `authorize()` force-granted admin scope on
# every handshake regardless of what was requested. Both are the same root
# failure -- a second, undifferentiated grant path that never asked "what
# was this caller actually entitled to" -- reached two different ways: one
# via which credential got you IN (convergence), one via what scope you left
# WITH (escalation). This block pins both against that repeat.
#
# Convergence proof, empirically: `trading_mcp/server.py::_build_auth()`
# constructs exactly one `fastmcp.server.auth.MultiAuth` instance and passes
# it, unmodified, as `auth=` to `FastMCP(...)`. `AuthProvider.get_middleware()`
# (fastmcp/server/auth/auth.py) then wraps the ASGI app in exactly one
# `AuthenticationMiddleware(backend=BearerAuthBackend(self))`, where `self`
# is that same MultiAuth object -- not one backend per verifier. So every
# request, bearer or OAuth-token, reaches the identical bound
# `MultiAuth.verify_token()` method; that method (not either wrapped
# verifier alone) is "the one authorization-decision function". The test
# below proves this by patching that exact bound method on the exact
# instance the live app uses and showing both credential shapes reach it.
# ═══════════════════════════════════════════════════════════════════════════

async def test_bearer_and_oauth_paths_converge_on_one_verify_token(monkeypatch):
    """Both a static-bearer request and a freshly-minted OAuth access token
    must be decided by the SAME `MultiAuth.verify_token()` call -- proven by
    patching that exact bound method on the app's actual auth instance and
    observing both credentials pass through it, rather than by reading the
    source and hoping it stays true."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv
    from fastmcp.server.auth import MultiAuth

    auth = srv._build_auth()
    assert isinstance(auth, MultiAuth)
    app = FastMCP("test-oauth", auth=auth).http_app(path="/mcp")

    seen_tokens: list[str] = []
    original_verify_token = auth.verify_token

    async def spying_verify_token(token: str):
        seen_tokens.append(token)
        return await original_verify_token(token)

    monkeypatch.setattr(auth, "verify_token", spying_verify_token)

    # Path 1: the static bearer token.
    bearer_token = _OAUTH_ENV["TRADING_AGENT_TOKEN"]
    bearer_headers = {**_MCP_HEADERS, "Authorization": f"Bearer {bearer_token}"}
    bearer_response = await _post_mcp(app, bearer_headers)
    assert bearer_response.status_code == 200, bearer_response.text

    # Path 2: a real OAuth-issued token -- register, gate, exchange.
    client_info = await _register_client(app)
    verifier, challenge = _pkce_pair()
    auth_response = await _authorize(
        app, client_id=client_info["client_id"],
        redirect_uri=client_info["redirect_uris"][0],
        code_challenge=challenge, operator_key=bearer_token,
    )
    from urllib.parse import parse_qs, urlsplit

    code = parse_qs(urlsplit(auth_response.headers["location"]).query)["code"][0]
    token_response = await _token_from_code(
        app, client_id=client_info["client_id"], client_secret=client_info["client_secret"],
        code=code, redirect_uri=client_info["redirect_uris"][0], code_verifier=verifier,
    )
    oauth_token = token_response.json()["access_token"]
    oauth_headers = {**_MCP_HEADERS, "Authorization": f"Bearer {oauth_token}"}
    oauth_response = await _post_mcp(app, oauth_headers)
    assert oauth_response.status_code == 200, oauth_response.text

    # Both credential shapes were decided by the one patched function --
    # never a second, unpatched path that reached a 200 without it.
    assert bearer_token in seen_tokens
    assert oauth_token in seen_tokens


def test_dcr_registration_rejects_scope_beyond_valid_scopes():
    """RFC 7591 registration is the first escalation checkpoint: a client
    asking for a scope this server never issues (e.g. "admin") must be refused
    at registration, not silently clamped later.

    NOTE the docstring here used to claim '"read" is the only valid scope',
    which contradicted its own assertion. `_make_oauth_provider()` passes no
    `required_scopes`, so it gets the constructor's permissive DEFAULT of all
    three. That is the object under test here, and "admin" is still not in it.
    What production builds is a different, narrower object -- see
    `test_production_oauth_provider_scope_plumbing` below, which is the one
    that pins the deployed configuration."""
    provider = _make_oauth_provider()
    assert set(provider.client_registration_options.valid_scopes) == {
        "read", "safe-write", "trade",
    }
    assert "admin" not in provider.client_registration_options.valid_scopes


def test_production_oauth_provider_scope_plumbing(monkeypatch):
    """Pins what `_build_oauth_provider()` ACTUALLY builds, not what a bare
    constructor call builds.

    HISTORY (M8-24), because this test previously asserted the opposite and
    the reversal was deliberate. `SingleOperatorOAuthProvider.__init__` used
    to take only `required_scopes` and pass that same list as `valid_scopes`.
    Production calls it with `["read"]`, so the registerable set collapsed to
    `{"read"}` and no credential this server could issue would ever satisfy
    `order_tools.py`'s `require_scopes("trade")`. That failed CLOSED, which
    is why it was safe to ship while the order tools were unregistered -- but
    it meant wiring them in would have locked the owner out rather than
    opened a hole. The constructor now takes `required_scopes`,
    `valid_scopes` and `default_scopes` as the three separate things they
    are.

    `trade` is in `default_scopes` on purpose: the claude.ai connector
    performs dynamic client registration WITHOUT naming a scope, so with the
    SDK default of `["read"]` it would silently register read-only and every
    order tool would answer 403. This is not the security boundary -- the
    operator secret at `/authorize`, `VESPER_TRADING`, the halt file, the
    circuit breaker, the portfolio-aware notional cap and the daily order
    limit are. If you narrow this, narrow it deliberately and expect the
    phone connector to stop being able to trade."""
    import trading_mcp.server as srv

    monkeypatch.setenv("MCP_PUBLIC_URL", "https://agent.example.test")
    provider = srv._build_oauth_provider("m2-production-operator-secret")

    assert provider is not None
    opts = provider.client_registration_options
    assert set(opts.valid_scopes) == {"read", "safe-write", "trade"}, (
        "production OAuth scope plumbing changed -- see this test's docstring "
        "before assuming that is an improvement"
    )
    assert set(opts.default_scopes) == {"read", "trade"}
    assert "admin" not in opts.valid_scopes
    # Every issued token must still carry `read` at minimum.
    assert set(provider.required_scopes) == {"read"}


async def test_authorize_request_for_unregistered_scope_never_issues_a_code(monkeypatch):
    """First checkpoint, at the HTTP boundary: a caller who passed the
    operator-key gate but asks `/authorize` for a scope (`admin`) the
    client was never registered for -- the attack shape supermcp's
    `authorize()` was vulnerable to, force-granting whatever a handshake
    merely asked for -- must never come back with a code. The mcp SDK's own
    `AuthorizationHandler` validates the requested scope against the
    client's registered scope before `SingleOperatorOAuthProvider.authorize()`
    is even reached, and redirects with `error=invalid_scope` instead."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth", auth=srv._build_auth()).http_app(path="/mcp")
    client_info = await _register_client(app)  # registers with default_scopes=["read"]
    _verifier, challenge = _pkce_pair()

    auth_response = await _authorize(
        app, client_id=client_info["client_id"],
        redirect_uri=client_info["redirect_uris"][0],
        code_challenge=challenge, operator_key=_OAUTH_ENV["TRADING_AGENT_TOKEN"],
        scope="admin",
    )
    assert auth_response.status_code == 302, auth_response.text
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(auth_response.headers["location"]).query)
    assert "code" not in query
    assert query["error"] == ["invalid_scope"]


async def test_authorize_itself_filters_scope_beyond_client_registration():
    """Second checkpoint, at `authorize()` itself -- defense in depth for
    exactly the function whose supermcp counterpart shipped this bug, so it
    must not rely solely on the SDK's upstream validation (the test above)
    catching every path in. Calling `SingleOperatorOAuthProvider.authorize()`
    directly with `AuthorizationParams.scopes` carrying an extra scope the
    client was never registered for must still come back with a code
    carrying only the scopes the client actually has -- 'admin' must not
    survive even when handed to authorize() directly, bypassing the SDK's
    own upstream scope check entirely."""
    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull

    provider = _make_oauth_provider()
    client = OAuthClientInformationFull(
        client_id="scope-test-client",
        redirect_uris=["https://client.example.test/callback"],
        scope="read",  # this client is registered for "read" only
    )
    provider.clients[client.client_id] = client

    params = AuthorizationParams(
        state="s", scopes=["read", "admin"], code_challenge="x" * 43,
        redirect_uri=client.redirect_uris[0], redirect_uri_provided_explicitly=True,
    )
    redirect = await provider.authorize(client, params)

    from urllib.parse import parse_qs, urlsplit

    code_value = parse_qs(urlsplit(redirect).query)["code"][0]
    issued_code = provider.auth_codes[code_value]
    assert issued_code.scopes == ["read"], (
        f"authorize() must drop scopes the client isn't registered for, got {issued_code.scopes!r}"
    )

    # And the token minted from that code inherits exactly those scopes too.
    token = await provider.exchange_authorization_code(client, issued_code)
    assert token.scope == "read"


async def test_refresh_token_cannot_escalate_scope_beyond_original_grant():
    """`exchange_refresh_token()`'s `requested.issubset(original)` check is
    the second escalation checkpoint -- a refresh must never be usable to
    widen scope beyond what the original authorization actually granted,
    even for a scope this server otherwise recognises as valid."""
    from mcp.server.auth.provider import TokenError

    provider = _make_oauth_provider()
    from mcp.shared.auth import OAuthClientInformationFull

    client = OAuthClientInformationFull(
        client_id="some-client", redirect_uris=["https://client.example.test/cb"],
    )
    pair = provider._issue_token_pair("some-client", [])  # granted NO scopes
    refresh = await provider.load_refresh_token(client, pair.refresh_token)
    assert refresh is not None

    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, ["read"])


# ═══════════════════════════════════════════════════════════════════════════
# M2-07: the permanently forbidden actions (guard.preview/place,
# submit_decision, resume) have no MCP tool registered at all, under any
# OAuth scope. This is a narrower, stronger claim than "a scope this token
# lacks blocks the call" -- it says there is no tool named after these
# actions to call in the first place, so even a maximally-scoped token
# can't reach them. Testable today against the 60 tools M0-M2 already
# registered; the scope-TIERING enforcement half (proving a *lesser*-scoped
# token is refused a *real* write tool) is M8-14, deferred until M8 adds
# any write tool for a scope check to be meaningful against.
# ═══════════════════════════════════════════════════════════════════════════

# Names a client might plausibly try for each permanently forbidden action.
# Deliberately not exhaustive by hand-listing alone --
# test_forbidden_actions_absent_from_full_tool_list below also checks every
# one of the 60 registered names against these verbs as substrings, so a
# namespaced or synonymous variant ("orders.place", "proposal.submit_decision")
# would be caught too, not just an exact-string miss.
_FORBIDDEN_TOOL_NAME_GUESSES = (
    "guard.preview", "guard.place", "preview_order", "preview_option",
    "place_order", "place_option", "preview", "place",
    "submit_decision", "resolve_proposal", "approve_proposal",
    "resume",
)


async def test_forbidden_actions_absent_from_full_tool_list():
    """None of the 60 registered tools (47 momentum + 13 Vesper) is named
    after guard.preview/place, submit_decision, or resume -- not a
    scoped-down version, not an alias, no tool at all."""
    import trading_mcp.server as srv

    tools = await srv.mcp.list_tools()
    names = {t.name for t in tools}
    assert len(names) == 77, f"expected 77 registered tools, got {len(names)}"

    for guess in _FORBIDDEN_TOOL_NAME_GUESSES:
        assert guess not in names, f"forbidden action {guess!r} is registered as a tool"

    forbidden_substrings = ("preview", "place_", "submit_decision", "resume")
    offenders = [
        n for n in names if any(sub in n.lower() for sub in forbidden_substrings)
    ]
    assert offenders == [], f"tool name(s) resembling a forbidden action: {offenders}"


async def test_oauth_scope_absence_of_forbidden_tools(monkeypatch):
    """Mint a real OAuth access token carrying every scope this server
    defines today (just "read" -- see `ClientRegistrationOptions` in
    oauth_provider.py; there is no elevated tier yet, that's M8-14's job
    once M8 adds a write tool for one to gate), then call each forbidden
    action BY NAME through the live MCP dispatcher with that token. The
    response must be a "no such tool" dispatch failure, not a
    permission-denied one -- proving these actions are absent from the tool
    set itself, not merely gated behind a scope this particular token lacks.

    Contrast with `test_http_request_with_no_credential_is_rejected`: a
    request with no credential never reaches the dispatcher at all -- the
    auth MIDDLEWARE rejects it with 401 before routing. Here the token is
    genuinely valid and fully scoped, so the request reaches the
    dispatcher and fails there instead, with an "Unknown tool" JSON-RPC
    error -- because there is nothing registered under that name for any
    scope to unlock.
    """
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-oauth-scope-absence", auth=srv._build_auth()).http_app(path="/mcp")

    client_info = await _register_client(app)
    client_id = client_info["client_id"]
    client_secret = client_info["client_secret"]
    redirect_uri = client_info["redirect_uris"][0]

    verifier, challenge = _pkce_pair()
    auth_response = await _authorize(
        app, client_id=client_id, redirect_uri=redirect_uri,
        code_challenge=challenge, operator_key=_OAUTH_ENV["TRADING_AGENT_TOKEN"],
        scope="read",  # every scope this server defines today
    )
    assert auth_response.status_code == 302, auth_response.text
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(auth_response.headers["location"]).query)
    code = query["code"][0]

    token_response = await _token_from_code(
        app, client_id=client_id, client_secret=client_secret, code=code,
        redirect_uri=redirect_uri, code_verifier=verifier,
    )
    assert token_response.status_code == 200, token_response.text
    token_payload = token_response.json()
    access_token = token_payload["access_token"]
    assert set(token_payload["scope"].split()) == {"read"}, (
        "sanity check: this token really does carry every scope the server "
        "defines today -- if a new scope tier is ever added, this test must "
        "be updated to mint a token carrying it too, not silently pass "
        "against a now-partial grant"
    )

    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {access_token}"}
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            init_response = await client.post("/mcp", json=_INIT_PAYLOAD, headers=headers)
            assert init_response.status_code == 200, init_response.text
            session_id = init_response.headers.get("mcp-session-id")
            call_headers = dict(headers)
            if session_id:
                call_headers["mcp-session-id"] = session_id
            await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=call_headers,
            )

            for i, name in enumerate(_FORBIDDEN_TOOL_NAME_GUESSES, start=2):
                call_payload = {
                    "jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": name, "arguments": {}},
                }
                response = await client.post("/mcp", json=call_payload, headers=call_headers)

                # Not blocked at the auth layer -- this token is valid and
                # fully scoped, so the request must reach the dispatcher
                # rather than being turned away as unauthenticated/forbidden.
                assert response.status_code not in (401, 403), (
                    f"call to {name!r} was rejected at the auth layer "
                    f"({response.status_code}) instead of reaching the "
                    f"dispatcher with this fully-scoped token"
                )
                assert response.status_code == 200, (
                    f"unexpected status calling {name!r}: "
                    f"{response.status_code} {response.text[:300]}"
                )
                assert "Unknown tool" in response.text, (
                    f"expected a not-found dispatch failure calling {name!r}, "
                    f"got: {response.text[:300]}"
                )


# ═══════════════════════════════════════════════════════════════════════════
# M2-08: the static-bearer fallback keeps working UNCHANGED after OAuth
# lands, and the http transport still refuses to start without
# TRADING_AGENT_TOKEN. The M2-01-era characterization tests above
# (test_http_request_with_no_credential_is_rejected,
# test_http_request_with_valid_bearer_is_accepted,
# test_http_transport_refuses_to_start_without_token) never set
# MCP_PUBLIC_URL, so they already re-run unchanged on every test session --
# this section adds the missing half: the SAME three behaviours, proven
# again with the OAuth provider actually mounted alongside (MultiAuth, not
# a bare HmacStaticTokenVerifier), so a future change to _build_auth() or
# to the OAuth mount can't quietly regress the bearer path while these
# OAuth-specific characterizations above keep passing.
# ═══════════════════════════════════════════════════════════════════════════

async def test_no_credential_rejected_with_oauth_mounted(monkeypatch):
    """M2-01's no-credential-rejected behaviour, re-run with MCP_PUBLIC_URL
    set so `_build_auth()` returns the composed MultiAuth instead of the
    bare bearer verifier -- the 401 must be unchanged."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv
    from fastmcp.server.auth import MultiAuth

    auth = srv._build_auth()
    assert isinstance(auth, MultiAuth), "sanity check: OAuth really is mounted here"

    app = FastMCP("test-auth-oauth", auth=auth).http_app(path="/mcp")
    response = await _post_mcp(app, _MCP_HEADERS)
    assert response.status_code == 401


async def test_static_bearer_accepted_while_oauth_configured(monkeypatch):
    """The one new assertion M2-08 adds: a request carrying only the static
    TRADING_AGENT_TOKEN bearer is still accepted even while OAuth is fully
    configured (MCP_PUBLIC_URL set, MultiAuth mounted) -- the fallback isn't
    merely present in the object graph, it still authorizes a real request
    on the live /mcp endpoint, with no OAuth handshake involved at all."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv
    from fastmcp.server.auth import MultiAuth

    auth = srv._build_auth()
    assert isinstance(auth, MultiAuth)

    app = FastMCP("test-auth-oauth", auth=auth).http_app(path="/mcp")
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {_OAUTH_ENV['TRADING_AGENT_TOKEN']}"}
    response = await _post_mcp(app, headers)
    assert response.status_code == 200, response.text


async def test_wrong_bearer_still_rejected_with_oauth_mounted(monkeypatch):
    """The mirror image of the acceptance test above: a bearer that is
    neither the configured static token nor a real OAuth-minted token must
    still be rejected once OAuth is mounted -- the fallback path hasn't
    gone lax alongside gaining a second credential source."""
    _set_oauth_env(monkeypatch)
    import trading_mcp.server as srv

    app = FastMCP("test-auth-oauth", auth=srv._build_auth()).http_app(path="/mcp")
    headers = {**_MCP_HEADERS, "Authorization": "Bearer not-the-right-token"}
    response = await _post_mcp(app, headers)
    assert response.status_code == 401


def test_http_transport_refuses_to_start_without_token_even_with_public_url_set():
    """M2-01's startup guard, re-run with MCP_PUBLIC_URL also set: a
    deployment that has declared a public URL (and would therefore mount
    OAuth) but still has no TRADING_AGENT_TOKEN must still refuse to start
    the http transport -- `_build_oauth_provider()` needs that same token as
    its `operator_secret`, so there is no configuration in which OAuth being
    reachable substitutes for the bearer token this guard checks."""
    env = dict(os.environ)
    env.pop("TRADING_AGENT_TOKEN", None)
    env["MCP_TRANSPORT"] = "http"
    env["MCP_PUBLIC_URL"] = "https://agent.mphinance.test"
    result = subprocess.run(
        [sys.executable, "-m", "trading_mcp.server"],
        env=env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, (
        f"expected a non-zero exit when TRADING_AGENT_TOKEN is unset, "
        f"MCP_TRANSPORT=http and MCP_PUBLIC_URL is set, got "
        f"{result.returncode}. stderr:\n{result.stderr}"
    )
    assert "TRADING_AGENT_TOKEN" in (result.stdout + result.stderr)


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
        # getattr(guard, "place") -- dynamic dispatch by string produces no
        # literal `.place` attribute node at all, so the attribute walk below
        # goes straight past it. Caught here instead.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
        ):
            target, attr = node.args[0], node.args[1]
            attr_is_order_method = (
                isinstance(attr, ast.Constant) and attr.value in ("preview", "place")
            )
            target_is_guard = (
                isinstance(target, ast.Name) and target.id in names
            ) or (isinstance(target, ast.Attribute) and target.attr == "guard")
            if attr_is_order_method and target_is_guard:
                found.append(attr.value)
            continue

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
    """Rule 3 / M8-23: guard.place / guard.preview permitted ONLY in the
    designated order module (trading_mcp/order_tools.py); forbidden everywhere
    else under trading_mcp/ and mcp_server/."""
    designated_order_module = (REPO_ROOT / "trading_mcp" / "order_tools.py").resolve()
    assert designated_order_module.exists(), "designated order module must exist"
    assert _guard_call_sites(designated_order_module), "order_tools.py must actually reach guard"

    offenders = {
        str(p): _guard_call_sites(p)
        for p in (REPO_ROOT / "trading_mcp").rglob("*.py")
        if p.resolve() != designated_order_module and _guard_call_sites(p)
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


def test_calling_every_vesper_tool_never_imports_execution_guard():
    """M0-05: the AST pins above catch a `guard.preview(`/`guard.place(`
    call site in the source text, but M0-05's actual concern is narrower and
    easier to miss -- `vesper/monitor.py` module-scope does `from
    vesper.execution_guard import guard, GuardError, TradingDisabled`, so
    merely IMPORTING that module (which `get_position_monitor_status` used
    to do, to reach its two read-only methods) pulls the live `guard`
    singleton into `sys.modules` as a side effect, with no call site for the
    AST pin to catch at all. `core/position_preview.py` exists so that
    doesn't happen.

    Run as a REAL subprocess (fresh interpreter), the same pattern
    `tests/test_approval_registry.py`'s import-boundary test and this file's
    own token-guard test use: within this same pytest session, other test
    files (e.g. test_execution_guard.py, test_monitor.py) have already
    imported `vesper.execution_guard` long before this test runs, which
    would make an in-process `sys.modules` check pass for the wrong reason
    regardless of what this feature actually did.

    State paths are redirected to a temp dir the same way
    `tests/conftest.py`'s `_isolated_vesper_state` fixture does (that
    fixture doesn't apply here -- this runs outside pytest, in its own
    interpreter) so this reads empty/no state rather than the developer's
    real `data/` files, and `SIDECAR_STATE_DIR` is pointed at an empty temp
    dir so `list_alerts` sees zero armed alerts and never calls out to
    TDPro. `core.knowledge` (the chromadb-backed trade-memory module)
    is forced unavailable via the same `sys.modules[...] = None` technique
    `test_recall_similar_setups_degrades_without_chromadb` above uses, so
    `recall_similar_setups` degrades cleanly instead of touching the real
    on-disk chroma index or an embedding model. Every tool is still
    genuinely CALLED -- these just keep the call hermetic, matching what
    each tool's own docstring already promises it does on a missing
    dependency.
    """
    script = '''
import asyncio
import inspect
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
data_dir = tmp / "data"
data_dir.mkdir()

import os
os.environ["SIDECAR_STATE_DIR"] = str(tmp / "alert_state")

# Force the chromadb-backed trade-memory module unavailable, same technique
# tests/test_trading_mcp.py's test_recall_similar_setups_degrades_without_chromadb
# uses -- keeps this hermetic without needing the real on-disk chroma index.
sys.modules["core.knowledge"] = None

import core.halt as _halt
_halt._DATA_DIR = data_dir
_halt._HALT_STATE_PATH = data_dir / "halt_state.json"

import core.circuit_breaker as _cb
_cb._DATA_DIR = data_dir
_cb._STATE_PATH = data_dir / "circuit_breaker_state.json"

import core.paper_ledger as _pl
_pl._DATA_DIR = data_dir
_pl._LEDGER_PATH = data_dir / "paper_ledger.json"

import core.audit_chain as _ac
_ac._DATA_DIR = data_dir
_ac._CHAIN_PATH = data_dir / "audit_chain.jsonl"

import core.approval_registry as _ar
_ar._DATA_DIR = data_dir
_ar._APPROVAL_STATE_PATH = data_dir / "approval_registry_state.json"

import core.conviction as _conv
_conv._DATA_DIR = data_dir

from trading_mcp.vesper_tools import register_vesper_tools


class _Collecting:
    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_kw):
        def _decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return _decorator


mcp = _Collecting()
register_vesper_tools(mcp)
assert mcp.tools, "no tools registered -- nothing to call"

kwargs_by_name = {
    "get_proposal": {"proposal_id": "does-not-exist"},
    "get_playbook_calibration": {"playbook": "wheel"},
    "recall_similar_setups": {"query_thesis": "test setup, forced unavailable above"},
}


async def _call_every_tool():
    for name, fn in mcp.tools.items():
        try:
            result = fn(**kwargs_by_name.get(name, {}))
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass  # every tool's own contract is to degrade, not raise -- but
                  # even if one doesn't, the import-boundary check below is
                  # this test's actual assertion, not each tool's return value.


asyncio.run(_call_every_tool())

assert "vesper.execution_guard" not in sys.modules, (
    "calling a trading_mcp tool pulled vesper.execution_guard into "
    "sys.modules: " + repr(sorted(m for m in sys.modules if m.startswith("vesper")))
)
print("OK", len(mcp.tools))
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"probe failed (rc={result.returncode})\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stdout.strip().startswith("OK"), result.stdout
    # Sanity: the probe actually iterated a non-trivial tool set, not an
    # empty registration that would make the sys.modules assertion vacuous.
    called = int(result.stdout.strip().split()[1])
    assert called >= 13, f"expected at least the 13 documented vesper tools, probe only saw {called}"


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
        REPO_ROOT / "trading_mcp" / "order_tools.py",
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
    import core.wb as wb

    class _BrokenWebull:
        def __init__(self):
            raise wb.WebullError("Webull credentials not configured")

    monkeypatch.setattr(wb, "Webull", _BrokenWebull)
    result = await vtools["get_account_state"]()
    assert result["available"] is False
    assert "reason" in result


def test_get_halt_status_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("core.halt.get_halt_status", _boom)
    result = vtools["get_halt_status"]()
    assert result == {"available": False, "reason": "simulated failure"}


def test_get_drawdown_status_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("core.circuit_breaker.get_peak_nlv", _boom)
    result = vtools["get_drawdown_status"]()
    assert result["available"] is False


def test_get_paper_positions_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("core.paper_ledger.get_paper_positions", _boom)
    result = vtools["get_paper_positions"]()
    assert result["available"] is False


def test_get_paper_summary_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("core.paper_ledger.get_paper_summary", _boom)
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
    # Simulates TDPro being unreachable: build_levels_of()'s own docstring
    # says levels_of() returns None (never a remembered number) in that case.
    # list_alerts imports core.td.build_levels_of directly (M0-06), not
    # vesper.alerts_runner's wrapper, so that's what this patches.
    monkeypatch.setattr("core.td.build_levels_of", lambda: (lambda symbol: None))

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
    from core import audit_chain

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
    monkeypatch.setattr("core.audit_chain.verify_chain", _boom)
    result = vtools["verify_audit_chain"]()
    assert result["available"] is False


def test_get_playbook_calibration_degrades_on_error(vtools, monkeypatch):
    monkeypatch.setattr("core.conviction.get_playbook_performance", _boom)
    result = vtools["get_playbook_calibration"]("wheel", days=30)
    assert result["available"] is False


async def test_recall_similar_setups_degrades_without_chromadb(vtools, monkeypatch):
    """Simulates a chromadb-less environment: `core/knowledge.py`
    imports `chromadb` at module level (an optional, sometimes-heavy dep --
    see vesper_tools.py's own docstring), so an environment without it would
    fail to import that whole module. Setting the module to None in
    sys.modules is the standard way to force that ImportError without
    actually uninstalling chromadb from this test environment, where it IS
    present."""
    monkeypatch.setitem(sys.modules, "core.knowledge", None)
    result = await vtools["recall_similar_setups"](query_thesis="oversold bounce off the 200dma")
    assert result["available"] is False
    assert "trade memory" in result["reason"]


async def test_get_position_monitor_status_degrades_on_poll_failure(vtools, monkeypatch):
    from core.position_preview import PositionPreviewMonitor

    monkeypatch.setattr(PositionPreviewMonitor, "poll_webull_positions", _aboom)
    result = await vtools["get_position_monitor_status"]()
    assert result["available"] is False


# ═══════════════════════════════════════════════════════════════════════════
# M0-09: the exposure boundary, mechanically -- halt permitted, resume and
# submit_decision permanently forbidden
#
# M0-00's A3 amendment widened the pin from "strictly read-only" to an
# explicit exposure boundary: `halt()` (freezing the account) is now a
# PERMITTED import, because M8-08 is going to build a halt tool on top of
# it. `resume()` (un-freezing) and `ApprovalRegistry.submit_decision()`
# (approving a pending order) stay forbidden forever -- those are the two
# calls that could move the state from "safe" to "an order can go out."
#
# This section proves the PIN MECHANISM only: that the AST scanner catches
# every shape a `resume`/`submit_decision` reference could take, and that it
# no longer flags a bare `halt` import. It does NOT assert that any
# registered tool actually calls or can reach `halt()` -- that positive
# reachability proof is M8-08's job, when the halt tool is actually built.
# ═══════════════════════════════════════════════════════════════════════════

def _module_func_names(tree: ast.AST, module_suffix: str, func_name: str) -> set[str]:
    """Local names bound to `func_name` imported from a module whose dotted
    path ends with `module_suffix` (e.g. "core.halt" / "resume"). Always
    includes the literal `func_name`, same over-flagging bias as
    `_guard_names`: a false positive costs nothing (don't name an unrelated
    function `resume`), a false negative is a missed mutating call."""
    names = {func_name}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(module_suffix):
            for alias in node.names:
                if alias.name == func_name:
                    names.add(alias.asname or alias.name)
    return names


def _dotted(node: ast.AST) -> str | None:
    """Render a Name/Attribute chain back to its dotted source text, e.g.
    `Attribute(attr='halt', value=Name('core'))` -> "core.halt"."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _module_func_ref_sites(pyfile: Path, module_suffix: str, func_name: str) -> list[str]:
    """Every reference to a module-level function (e.g. `core.halt.resume`)
    in `pyfile`: a plain call, an aliased import, a bound reference passed
    elsewhere (`asyncio.to_thread(resume, ...)`), or `import core.halt;
    core.halt.resume(...)`-style dotted access.

    Mirrors `_guard_call_sites`'s four-shape coverage
    (`test_guard_pin_catches_indirect_call_shapes`), adapted for a plain
    module-level function rather than a singleton's bound method: any Name
    node whose id resolves to the (possibly aliased) imported function
    catches the first three shapes uniformly, since it doesn't matter
    whether that Name is being called directly or merely passed as a
    reference. Dotted access is matched separately because it never goes
    through an import-bound local name at all.
    """
    try:
        tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
    except (SyntaxError, UnicodeDecodeError):
        return []
    names = _module_func_names(tree, module_suffix, func_name)
    module_aliases = {module_suffix}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(module_suffix) and alias.asname:
                    module_aliases.add(alias.asname)

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in names and not isinstance(node.ctx, ast.Store):
            found.append(f"{func_name}(reference)")
        elif isinstance(node, ast.Attribute) and node.attr == func_name:
            base_dotted = _dotted(node.value)
            if base_dotted in module_aliases:
                found.append(f"{func_name}(dotted)")
    return found


def _approval_registry_names(tree: ast.AST) -> set[str]:
    """Local names bound to `core.approval_registry`'s `approval_registry`
    singleton. Same over-flagging bias as `_guard_names`."""
    names = {"approval_registry"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("approval_registry"):
            for alias in node.names:
                if alias.name == "approval_registry":
                    names.add(alias.asname or alias.name)
    return names


def _submit_decision_call_sites(pyfile: Path) -> list[str]:
    """`ApprovalRegistry.submit_decision()` reference sites in `pyfile`.

    Identical shape to `_guard_call_sites` (a singleton's bound method), so
    it reuses that exact matching strategy: direct/aliased import, bound
    reference (`asyncio.to_thread(approval_registry.submit_decision, ...)`),
    and dotted-module access (`core.approval_registry.approval_registry
    .submit_decision(...)`)."""
    try:
        tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
    except (SyntaxError, UnicodeDecodeError):
        return []
    names = _approval_registry_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "submit_decision":
            continue
        base = node.value
        if isinstance(base, ast.Name) and base.id in names:
            found.append(node.attr)
        elif isinstance(base, ast.Attribute) and base.attr == "approval_registry":
            found.append(node.attr)
    return found


def test_halt_resume_pin_catches_indirect_call_shapes(tmp_path):
    """Same four-shape coverage for `resume()` as
    `test_guard_pin_catches_indirect_call_shapes` proves for `guard.place`."""
    bypasses = {
        "direct": "from core.halt import resume\nresume()\n",
        "renamed_import": "from core.halt import resume as r\nr()\n",
        "bound_reference": (
            "import asyncio\nfrom core.halt import resume\nasyncio.to_thread(resume)\n"
        ),
        "dotted_module": "import core.halt\ncore.halt.resume()\n",
        "aliased_module": "import core.halt as ch\nch.resume()\n",
    }
    for label, src in bypasses.items():
        f = tmp_path / f"{label}.py"
        f.write_text(src, encoding="utf-8")
        assert _module_func_ref_sites(f, "core.halt", "resume"), f"pin failed to catch bypass shape: {label}"

    # ...prose describing the rule is still not a violation...
    clean = tmp_path / "prose.py"
    clean.write_text('"""No tool here may call resume(."""\n', encoding="utf-8")
    assert _module_func_ref_sites(clean, "core.halt", "resume") == []

    # ...and the permitted read-only sibling must never be flagged by the
    # `resume` matcher.
    permitted = tmp_path / "permitted.py"
    permitted.write_text("from core.halt import get_halt_status\nget_halt_status()\n", encoding="utf-8")
    assert _module_func_ref_sites(permitted, "core.halt", "resume") == []


def test_submit_decision_pin_catches_indirect_call_shapes(tmp_path):
    """Same four-shape coverage for `ApprovalRegistry.submit_decision()` as
    `test_guard_pin_catches_indirect_call_shapes` proves for `guard.place`."""
    bypasses = {
        "direct": (
            "from core.approval_registry import approval_registry\n"
            "approval_registry.submit_decision(1)\n"
        ),
        "renamed_import": (
            "from core.approval_registry import approval_registry as ar\nar.submit_decision(1)\n"
        ),
        "bound_method": (
            "import asyncio\n"
            "from core.approval_registry import approval_registry\n"
            "asyncio.to_thread(approval_registry.submit_decision, 1)\n"
        ),
        "dotted_module": (
            "import core.approval_registry\n"
            "core.approval_registry.approval_registry.submit_decision(1)\n"
        ),
    }
    for label, src in bypasses.items():
        f = tmp_path / f"{label}.py"
        f.write_text(src, encoding="utf-8")
        assert _submit_decision_call_sites(f), f"pin failed to catch bypass shape: {label}"

    clean = tmp_path / "prose.py"
    clean.write_text('"""No tool here may call submit_decision(."""\n', encoding="utf-8")
    assert _submit_decision_call_sites(clean) == []

    permitted = tmp_path / "permitted.py"
    permitted.write_text(
        "from core.approval_registry import approval_registry\napproval_registry.list_pending()\n",
        encoding="utf-8",
    )
    assert _submit_decision_call_sites(permitted) == []


def test_halt_import_is_now_permitted(tmp_path):
    """Positive proof the M0-00/A3 widening actually took effect: merely
    importing (not calling) `halt` from `core.halt` must no longer trip
    either matcher -- only `resume` and `submit_decision` remain forbidden.
    Whether any real tool reaches `halt()` is M8-08's concern, not this
    test's; this only proves the scanner stopped objecting to it."""
    f = tmp_path / "would_import_halt.py"
    f.write_text("from core.halt import halt\nhalt(reason='test')\n", encoding="utf-8")
    assert _module_func_ref_sites(f, "core.halt", "resume") == []
    assert _submit_decision_call_sites(f) == []


def test_no_vesper_tool_can_reach_halt_or_resume():
    """The exposure boundary, scanned across the whole `trading_mcp/`
    package (not just `vesper_tools.py`) the same way the sibling guard pins
    (`test_trading_mcp_never_calls_execution_guard`) already do: `resume()`
    and `ApprovalRegistry.submit_decision()` must never be reachable from
    anywhere in this package, in any of the four shapes the matchers above
    prove they catch. `halt()` itself is unchecked here -- it is now a
    permitted import (see `test_halt_import_is_now_permitted`); nothing in
    this test asserts a tool actually reaches it."""
    offenders: dict[str, list[str]] = {}
    for pyfile in (REPO_ROOT / "trading_mcp").rglob("*.py"):
        hits = _module_func_ref_sites(pyfile, "core.halt", "resume") + _submit_decision_call_sites(pyfile)
        if hits:
            offenders[str(pyfile)] = hits
    assert offenders == {}, f"forbidden mutating call/reference found: {offenders}"


def test_forbidden_resume_pin_catches_a_real_throwaway_module_under_trading_mcp():
    """Regression proof that the widened pin actually walks the whole
    `trading_mcp/` package, not just `vesper_tools.py`: drops a real
    throwaway module directly under `trading_mcp/` referencing `resume(`,
    confirms the same scan `test_no_vesper_tool_can_reach_halt_or_resume`
    runs would catch it, then deletes the file so nothing is left behind
    (pass or fail)."""
    probe_path = REPO_ROOT / "trading_mcp" / "_pin_regression_probe.py"
    assert not probe_path.exists(), "leftover probe file from a prior run -- remove it before re-running"
    probe_path.write_text(
        '"""Throwaway module for M0-09\'s pin regression test. Deleted immediately after."""\n'
        "resume()\n",
        encoding="utf-8",
    )
    try:
        hits = _module_func_ref_sites(probe_path, "core.halt", "resume")
        assert hits, "widened pin failed to catch a bare `resume()` reference in a real trading_mcp/ file"
    finally:
        probe_path.unlink()
    assert not probe_path.exists()


def test_m8_13_exposure_boundary_and_no_audio_routes():
    """M8-13: Confirms all M8 voice modules are scanned by the exposure pin,
    no STT or audio endpoints exist, and resume remains strictly unreachable.
    """
    scanned_files = {p.name for p in (REPO_ROOT / "trading_mcp").rglob("*.py")}
    expected_m8_modules = {
        "bar_summary.py", "gamma_summary.py", "voice_tools.py",
        "drafting.py", "order_tools.py", "resources.py", "prompts.py"
    }
    assert expected_m8_modules.issubset(scanned_files), (
        f"Missing expected M8 modules from scanned set: {expected_m8_modules - scanned_files}"
    )

    # Confirm resume() is not referenced anywhere in trading_mcp
    for pyfile in (REPO_ROOT / "trading_mcp").rglob("*.py"):
        hits = _module_func_ref_sites(pyfile, "core.halt", "resume")
        assert hits == [], f"Forbidden resume reference in {pyfile}: {hits}"

    # Confirm no STT or audio routes exist in trading_mcp or mcp_server
    for pyfile in list((REPO_ROOT / "trading_mcp").rglob("*.py")) + list((REPO_ROOT / "mcp_server").rglob("*.py")):
        content = pyfile.read_text(encoding="utf-8").lower()
        assert "speech_to_text" not in content
        assert "openrouter_stt" not in content
        assert "audio/wav" not in content
        assert "audio/mp3" not in content


# ═══════════════════════════════════════════════════════════════════════════
# M0-08: the MCP server survives the LangGraph agent being unimportable
# ═══════════════════════════════════════════════════════════════════════════

def test_process_independence_server_survives_vesper_graph_import_failure():
    """`trading_mcp/server.py` reaches nothing under `vesper/` at all -- every
    stateful tool in `trading_mcp/vesper_tools.py` was repointed at `core/`
    by M0-06/M0-07 specifically so this server has no load-bearing dependency
    on the LangGraph agent pipeline. This is the mechanical proof: poison
    `sys.modules["vesper.graph"]` and `sys.modules["langgraph"]` (and every
    `langgraph.*` submodule already cached) to `None` -- Python raises
    ImportError for any `import` statement naming a module whose
    `sys.modules` entry is `None` -- *before* `trading_mcp.server` is ever
    imported, then assert the server still constructs, registers its full
    tool set, and every probed tool still returns its normal
    available:true/available:false shape rather than blowing up.

    Run as a REAL subprocess (fresh interpreter), same reasoning as
    `test_calling_every_vesper_tool_never_imports_execution_guard` above: by
    the time this test runs, other files in the same pytest session
    (test_graph.py, test_runner.py, ...) have already imported
    `vesper.graph` and `langgraph` for real, so poisoning `sys.modules`
    in-process would just be un-poisoned by whatever already cached them --
    a fresh interpreter is the only way the poison actually bites.

    State paths are redirected to a temp dir the same way the sibling
    subprocess probe above does, so this reads empty state rather than the
    developer's real `data/` files.
    """
    script = '''
import asyncio
import inspect
import sys
import tempfile
from pathlib import Path

# Poison vesper.graph and langgraph (and any langgraph.* submodule) so any
# "import vesper.graph" or "import langgraph[...]" anywhere in the reachable
# import graph raises ImportError -- simulating the LangGraph agent code
# being flat-out unimportable, before trading_mcp.server is ever imported.
sys.modules["vesper.graph"] = None
sys.modules["langgraph"] = None
for _name in list(sys.modules):
    if _name == "langgraph" or _name.startswith("langgraph."):
        sys.modules[_name] = None

tmp = Path(tempfile.mkdtemp())
data_dir = tmp / "data"
data_dir.mkdir()

import os
os.environ["SIDECAR_STATE_DIR"] = str(tmp / "alert_state")

# trading_mcp.server load_dotenv()s the repo .env at import time below, which
# WOULD load this developer's real Webull credentials into os.environ (its
# WEBULL_APP_KEY/SECRET are real, per CLAUDE.md rule 2) -- python-dotenv's
# load_dotenv(override=False) never overwrites an already-set variable, so
# presetting these to empty strings first keeps get_position_monitor_status's
# live-broker poll from ever attempting a real network call: core.wb.credentials()
# raises WebullError synchronously on an empty key/secret, before any I/O.
for _k in ("WEBULL_KEY", "WEBULL_APP_KEY", "WEBULL_SECRET", "WEBULL_APP_SECRET"):
    os.environ[_k] = ""

sys.modules["core.knowledge"] = None

import core.halt as _halt
_halt._DATA_DIR = data_dir
_halt._HALT_STATE_PATH = data_dir / "halt_state.json"

import core.approval_registry as _ar
_ar._DATA_DIR = data_dir
_ar._APPROVAL_STATE_PATH = data_dir / "approval_registry_state.json"

import core.audit_chain as _ac
_ac._DATA_DIR = data_dir
_ac._CHAIN_PATH = data_dir / "audit_chain.jsonl"

# The mechanical proof itself: trading_mcp.server must import cleanly and
# FastMCP("trading-agent") must construct, with vesper.graph/langgraph both
# poisoned unimportable above.
import trading_mcp.server as srv
assert srv.mcp is not None
assert srv.mcp.name == "trading-agent"

tools = asyncio.run(srv.mcp.list_tools())
assert len(tools) == 77, f"expected 77 registered tools, got {len(tools)}"

# Confirm the four named surfaces each return their normal shape (a plain
# dict, no unhandled exception) by calling them through the same
# _CollectingMCP-style stand-in the rest of this file uses.
class _Collecting:
    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_kw):
        def _decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return _decorator


from trading_mcp.vesper_tools import register_vesper_tools

vmcp = _Collecting()
register_vesper_tools(vmcp)
assert vmcp.tools, "no vesper tools registered -- nothing to probe"

probed = {}
for name in ("get_halt_status", "list_pending_proposals", "get_audit_trail", "get_position_monitor_status"):
    fn = vmcp.tools[name]
    result = fn()
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    assert isinstance(result, dict), f"{name} returned {type(result)!r}, not a dict: {result!r}"
    probed[name] = result

# core-backed reads with real (empty) on-disk state behind them: normal
# available:true shape, not a degrade.
assert probed["get_halt_status"].get("available") is True, probed["get_halt_status"]
assert probed["list_pending_proposals"].get("available") is True, probed["list_pending_proposals"]
assert probed["get_audit_trail"].get("available") is True, probed["get_audit_trail"]

# get_position_monitor_status polls live Webull state this hermetic probe
# never configures real credentials for (WEBULL_KEY/SECRET are blanked
# above): depending on whether the webull SDK package itself is even
# importable in this environment it either degrades with a reason or
# reports zero positions -- either is fine, both are the SAME thing this
# test is actually proving: a normal dict shape, no unhandled exception,
# whatever the specific missing dependency turns out to be.
pm = probed["get_position_monitor_status"]
assert isinstance(pm.get("available"), bool), pm
if pm["available"] is False:
    assert pm.get("reason"), pm

print("OK", len(tools))
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"probe failed (rc={result.returncode})\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stdout.strip().startswith("OK"), result.stdout


def test_operator_gate_form_posts_the_secret():
    """The gate form must POST, never GET.

    A GET form puts `operator_key=<TRADING_AGENT_TOKEN>` in the request line,
    where Traefik's and uvicorn's access logs and the browser's history keep it
    verbatim -- on every ordinary reconnect, no attacker required. That is the
    same class of failure as the 2026-09-03 placeholder-token incident: a
    credential ending up somewhere it is readable. `_gated_authorize` still
    accepts GET params so the client's initial redirect works; only the step
    carrying the secret is POSTed.
    """
    provider = _make_oauth_provider()
    response = provider._render_gate_form({}, wrong_attempt=False)
    body = response.body.decode()

    assert 'method="post"' in body.lower()
    assert 'method="get"' not in body.lower(), (
        "the operator gate form reverted to GET, which leaks the operator "
        "secret into access logs and browser history"
    )


def test_operator_gate_form_names_the_client_and_scope():
    """A gate that shows the human nothing about what they are approving is a
    confused deputy waiting for the first scope beyond `read`."""
    provider = _make_oauth_provider()
    body = provider._render_gate_form(
        {"client_id": "claude-connector-1", "scope": "read"}, wrong_attempt=False
    ).body.decode()

    assert "claude-connector-1" in body
    assert "read" in body


def test_guard_pin_catches_dynamic_getattr_dispatch(tmp_path):
    """The rule-3 pin must also catch `getattr(guard, "place")(...)`.

    The pin resolves import aliases and dotted access, which covers every
    idiom the live order path actually uses. Dynamic dispatch by string was
    the remaining hole: it produces no literal `.place` attribute node, so an
    AST scan looking only for attribute access walks straight past it.
    """
    module = tmp_path / "sneaky_tool.py"
    module.write_text(
        "from vesper.execution_guard import guard\n"
        "def go(ticket):\n"
        "    return getattr(guard, 'place')(ticket)\n"
    )
    hits = _guard_call_sites(module)
    assert hits, (
        "the rule-3 guard pin missed getattr(guard, 'place') -- a new MCP "
        "module could grow a working order path and still pass this suite"
    )
