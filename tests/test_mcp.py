"""The Claude Desktop MCP surface.

The load-bearing assertion here is negative: no tool that could place an order
may ever appear. sidecar is read-only (CLAUDE.md rule 3) and MCP is the easiest
place for that to erode, because adding a tool is a five-line change.
"""

from __future__ import annotations

import json

import pytest

mcp_module = pytest.importorskip("mcp_server", reason="needs the `mcp` package")

ORDER_WORDS = ("order", "buy", "sell", "place", "trade", "cancel", "execute", "submit")


async def tools():
    return await mcp_module.mcp.list_tools()


@pytest.mark.asyncio
async def test_no_order_shaped_tool_is_exposed():
    names = [t.name for t in await tools()]
    offenders = [n for n in names if any(w in n.lower() for w in ORDER_WORDS)]
    assert not offenders, f"sidecar is read-only; remove {offenders}"


@pytest.mark.asyncio
async def test_the_expected_tools_are_registered():
    names = {t.name for t in await tools()}
    assert {"get_portfolio", "get_gamma", "get_signals", "list_alerts",
            "create_alert", "delete_alert", "test_alert_delivery"} <= names


@pytest.mark.asyncio
async def test_create_alert_accepts_a_number_or_a_named_level():
    """A model asked for 'when SPY breaks 743' sends a NUMBER.

    Typed as `str` alone this failed schema validation before ever reaching the
    server, which is exactly how it shipped the first time.
    """
    tool = next(t for t in await tools() if t.name == "create_alert")
    level = tool.input_schema["properties"]["level"]
    rendered = json.dumps(level)
    assert "string" in rendered and "number" in rendered


@pytest.mark.asyncio
async def test_create_alert_requires_the_three_things_that_define_an_alert():
    tool = next(t for t in await tools() if t.name == "create_alert")
    assert set(tool.input_schema["required"]) == {"symbol", "level", "direction"}


@pytest.mark.asyncio
async def test_instructions_state_that_it_cannot_trade():
    text = (mcp_module.mcp.instructions or "").lower()
    assert "cannot place" in text or "cannot" in text and "order" in text


@pytest.mark.asyncio
async def test_instructions_frame_gamma_as_positioning_not_a_forecast():
    """The failure mode is a level being read back as a price target."""
    text = (mcp_module.mcp.instructions or "").lower()
    assert "not a forecast" in text


@pytest.mark.asyncio
async def test_an_unreachable_sidecar_names_the_url_and_the_likely_cause(monkeypatch):
    monkeypatch.setattr(mcp_module, "BASE", "http://127.0.0.1:1")   # nothing listens
    result = await mcp_module.mcp.call_tool("get_gamma", {"symbol": "SPY"})
    text = json.dumps(result.content[0].text if hasattr(result, "content") else str(result))
    assert "127.0.0.1:1" in text and "tailnet" in text
