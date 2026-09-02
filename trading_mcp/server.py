"""
trading_mcp.server: owner-only MCP server exposing this repo's read-only
trading tooling to Claude.

PHASE 0 — READ-ONLY ONLY. No tool registered here may call
guard.preview(), guard.place(), halt()/resume(), or
ApprovalRegistry.submit_decision(). The order path stays exactly where
CLAUDE.md rule 3 puts it: vesper/execution_guard.py, reached only from
inside the LangGraph executor node after a Telegram/Discord approval.
"Any adapter that grows its own order path is a new threat model, not a
small addition" — this server does not grow one.

This is a SEPARATE process from mcp_server/server.py (the existing stdio
"momentum" server, left completely alone — its no-broker-credentials
property is load-bearing) and from supermcp (a different, subscriber-facing
server on another host that this package never talks to).

Standalone:
    .venv/bin/python -m trading_mcp.server

Transport is chosen by MCP_TRANSPORT (default "stdio", no auth needed since
stdio carries no headers). An "http" transport REQUIRES TRADING_AGENT_TOKEN
to be set and binds to 127.0.0.1 only — see CLAUDE.md rule 1: loopback or
Tailscale, never 0.0.0.0.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth

from trading_mcp.auth import HmacStaticTokenVerifier
from trading_mcp.oauth_provider import SingleOperatorOAuthProvider

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _build_oauth_provider(token: str) -> SingleOperatorOAuthProvider | None:
    """Build the OAuth 2.1 authorization server (M2-03), or None if this
    deployment hasn't set a public URL for it yet.

    `MCP_PUBLIC_URL` (e.g. `https://agent.mphinance.com`) is required because
    the OAuth metadata this mounts (issuer, authorization_endpoint,
    token_endpoint, ...) has to advertise URLs a real client can reach —
    there is no sane default for that on a box that isn't deployed yet. Local
    dev and the test suite simply don't set it, so `_build_auth()` falls back
    to the bearer-only path below, unchanged from M2-01/M2-02.
    """
    base_url = os.environ.get("MCP_PUBLIC_URL")
    if not base_url:
        return None
    return SingleOperatorOAuthProvider(
        operator_secret=token,
        base_url=base_url,
        required_scopes=["read"],
    )


def _build_auth() -> HmacStaticTokenVerifier | MultiAuth | None:
    """Build the auth provider from TRADING_AGENT_TOKEN, or None if unset.

    Constructing the server is unconditional either way — a missing token
    never crashes startup, since stdio (the default transport) carries no
    headers and an auth check there would be meaningless. An http transport
    with no token is refused later, at the bottom of this module, rather
    than silently serving unauthenticated.

    Uses `HmacStaticTokenVerifier` (trading_mcp/auth.py), not fastmcp's own
    `StaticTokenVerifier` — see that module's docstring: the stock verifier
    compares tokens with a plain dict lookup, which is not constant-time.

    When `MCP_PUBLIC_URL` is also set, wraps the bearer verifier and the
    OAuth 2.1 provider (M2-03, `trading_mcp/oauth_provider.py`) in a single
    `MultiAuth`, per app_spec.txt's requirement that "both auth paths
    converge on one authorization check" — `MultiAuth.verify_token()` is that
    one check: it tries the OAuth server's own token store first, then the
    static bearer, and a token is valid if either says so. There are not two
    independent authorization decisions here, only one, fed by two sources.

    M2-06: this convergence is load-bearing, not incidental, and here is
    exactly why. `fastmcp`'s `AuthProvider.get_middleware()` wraps the ASGI
    app in exactly one `AuthenticationMiddleware(backend=BearerAuthBackend(
    self))`, where `self` is whatever single object this function returns —
    never one backend per credential type. So every request this server
    receives, whether it carries `TRADING_AGENT_TOKEN` or an OAuth-minted
    access token, is decided by the identical bound `MultiAuth.verify_token`
    call constructed right here; there is no second code path a future
    change could add that reaches the MCP tools without going through it.
    That property is exactly what supermcp's `/login` bug (see
    docs/AUTH_TRADE_SCOPE_LOCKDOWN.md) lacked: its password-based branch
    handed back `config.SUPERMCP_TOKEN` — the admin credential — directly,
    a second grant path that never ran through `require_trade_scope()` at
    all. If this function is ever changed to build more than one `AuthProvider`
    and pass them to more than one `FastMCP`/route, or to hand-roll a second
    header check anywhere in this module, that is this exact bug again:
    don't. `tests/test_trading_mcp.py`'s
    `test_bearer_and_oauth_paths_converge_on_one_verify_token` pins this by
    patching the live `MultiAuth.verify_token` bound method and proving both
    credential shapes are observed by it, empirically, not by re-reading
    this comment.

    Convergence at the credential-verification layer is only half of M2-06.
    The other half — that an OAuth-issued token can never carry more scope
    than it was granted — lives in `oauth_provider.py`'s `authorize()`; see
    the comment there for the matching half of supermcp's bug (its
    `authorize()` force-granted admin scope on every OAuth handshake, no
    matter what was requested).
    """
    token = os.environ.get("TRADING_AGENT_TOKEN")
    if not token:
        return None
    bearer = HmacStaticTokenVerifier(
        tokens={token: {"client_id": "owner", "scopes": ["read", "safe-write"]}},
        required_scopes=["read"],
    )
    oauth = _build_oauth_provider(token)
    if oauth is None:
        return bearer
    return MultiAuth(server=oauth, verifiers=[bearer])


mcp = FastMCP(
    "trading-agent",
    auth=_build_auth(),
    instructions=(
        "Owner-only view into the webull-sidecar / Vesper trading "
        "agent: momentum/options/screener analytics (mcp_server, tiers 1-3) "
        "plus Vesper's own account, halt, alert, approval-queue and "
        "conviction-journal state. No tool here can place, preview, or "
        "approve an order — halt is permitted as a safe risk-reducing control, while "
        "orders move only through Vesper's own Telegram/Discord approval "
        "flow. Use this server to answer questions about current state, "
        "never to act on it."
    ),
)


# ── Tool registration ───────────────────────────────────────────────────────────
def _register_all_tools() -> int:
    """Register every read-only tool this server exposes; return the total count."""
    from mcp_server.registry import register_momentum_tools

    momentum_tools = register_momentum_tools(mcp, include_tiers=(1, 2, 3))
    logger.info("Registered %d momentum tools (tiers 1-3)", len(momentum_tools))

    from trading_mcp.vesper_tools import register_vesper_tools

    vesper_tools = register_vesper_tools(mcp)
    logger.info("Registered %d vesper read-only tools", len(vesper_tools))

    total = len(momentum_tools) + len(vesper_tools)
    logger.info("trading-agent MCP server: %d tools registered total", total)
    return total


_register_all_tools()


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if transport == "http":
        # CLAUDE.md rule 1: loopback or Tailscale, never 0.0.0.0 — this is
        # the only default in the whole repo permitted to bind a port, and
        # it defaults to loopback. An operator who wants Tailscale reach has
        # to override MCP_HOST explicitly; nothing here does it for them.
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "8402"))

        if not os.environ.get("TRADING_AGENT_TOKEN"):
            logger.warning(
                "http transport requested but TRADING_AGENT_TOKEN is not set "
                "— refusing to start an unauthenticated network listener."
            )
            raise SystemExit(1)

        import asyncio

        logger.info(
            "Starting trading-agent MCP server on %s:%s/mcp (transport=streamable-http)...",
            host, port,
        )
        asyncio.run(
            mcp.run_http_async(
                transport="streamable-http", host=host, port=port, path="/mcp"
            )
        )
    else:
        logger.info("Starting trading-agent MCP server (transport=stdio)...")
        mcp.run()
