"""Pins for the TickerTrace tools — the free surface a fork cannot reproduce.

The rest of the free tool surface is code over public data: real work, but
anyone can rewrite it. These 17 read a diff of daily ETF holdings accumulated
since 2026-02, so a competitor who forks the repo gets the client and none of
the history. That makes them the load-bearing part of the free tier, which is
why they get tests rather than being trusted to a docstring.

Hermetic: every HTTP call is stubbed. The suite must pass with no network.
"""

from __future__ import annotations

import httpx
import pytest

from core import tickertrace as tt
from mcp_server.tickertrace_tools import register_tickertrace_tools


class _FakeMCP:
    """Minimal stand-in for FastMCP: records what `@mcp.tool()` decorates."""

    def __init__(self):
        self.registered: dict[str, object] = {}

    def tool(self):
        def decorator(fn):
            self.registered[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def captured(monkeypatch):
    """Capture the path and params of the last `_fetch`, without touching the network."""
    calls: list[tuple[str, dict]] = []

    def fake_fetch(path, params=None):
        calls.append((path, params or {}))
        return {"ok": True}

    monkeypatch.setattr(tt, "_fetch", fake_fetch)
    return calls


def test_all_seventeen_register():
    mcp = _FakeMCP()
    names = register_tickertrace_tools(mcp)
    assert len(names) == 17
    assert set(names) == set(mcp.registered), "returned names must match what registered"


def test_every_tool_is_etf_prefixed():
    """The prefix is load-bearing, not cosmetic — see the collision test below."""
    mcp = _FakeMCP()
    for name in register_tickertrace_tools(mcp):
        assert name.startswith("etf_"), f"{name} would sit unprefixed beside momentum tools"


def test_the_two_real_collisions_are_avoided():
    """`get_signals` and `get_sector_flow` already exist on this server as
    TraderMatrix options-flow tools. Same words, different data: one is options
    premium by sector, the other is institutional fund flow. Registering both
    unprefixed is a silent collision, and a model choosing by name could not
    tell which dataset it was getting."""
    mcp = _FakeMCP()
    names = set(register_tickertrace_tools(mcp))
    assert "get_signals" not in names
    assert "get_sector_flow" not in names
    assert {"etf_signals", "etf_sector_flow"} <= names


def test_tools_hit_the_expected_endpoints(captured):
    mcp = _FakeMCP()
    register_tickertrace_tools(mcp)

    mcp.registered["etf_briefing"]()
    mcp.registered["etf_divergences"]()
    mcp.registered["etf_global_stats"]()
    paths = [p for p, _ in captured]
    assert paths == ["/api/v1/briefing", "/api/v1/divergences", "/api/v1/stats"]


def test_ticker_and_fund_lookups_uppercase_their_argument(captured):
    mcp = _FakeMCP()
    register_tickertrace_tools(mcp)

    mcp.registered["etf_stock_activity"]("nvda")
    mcp.registered["etf_fund_detail"]("arkk")
    assert captured[0][0] == "/api/v1/stock/NVDA"
    assert captured[1][0] == "/api/v1/fund/ARKK"


def test_optional_filters_reach_the_request(captured):
    mcp = _FakeMCP()
    register_tickertrace_tools(mcp)

    mcp.registered["etf_holdings_changes"](provider="ARK Invest", direction="buying")
    path, params = captured[0]
    assert path == "/api/v1/changes"
    assert params["provider"] == "ARK Invest"
    assert params["direction"] == "buying"


def test_none_params_are_dropped_before_the_request(monkeypatch):
    """The API treats an absent filter and an explicit null differently, and every
    tool here passes optional filters through as None by default."""
    seen: dict = {}

    class _Resp:
        def raise_for_status(self): return None
        def json(self): return {"ok": True}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None):
            seen["params"] = params
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    tt.get_holdings_changes(provider=None, fund="ARKK", direction=None)
    assert "provider" not in seen["params"]
    assert "direction" not in seen["params"]
    assert seen["params"]["fund"] == "ARKK"


def test_an_outage_degrades_instead_of_raising(monkeypatch):
    """Every caller is an MCP tool. One that raises takes the whole call down;
    one that reports unavailability lets the model use the other 60."""
    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "Client", _Client)
    result = tt.get_global_stats()
    assert result["available"] is False
    assert "unreachable" in result["error"]


def test_client_sends_no_credential():
    """api.tickertrace.pro is deliberately open — 'fully open, no key required'
    is the product's own stated position. This client must not grow an auth
    header; if it ever needs one, that is a product decision, not a patch."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "core" / "tickertrace.py").read_text()
    for forbidden in ("Authorization", "X-API-Key", "api_key", "Bearer"):
        assert forbidden not in source, f"{forbidden} appeared in an intentionally open client"


def test_registry_includes_tickertrace_by_default():
    from mcp_server.registry import register_momentum_tools
    import inspect

    sig = inspect.signature(register_momentum_tools)
    assert sig.parameters["include_tickertrace"].default is True
