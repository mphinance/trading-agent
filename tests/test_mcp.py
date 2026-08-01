"""The Claude Desktop MCP surface.

This file used to carry the opposite assertion: that no order-shaped tool may
ever appear, because sidecar was read-only. Rule 3 was reversed deliberately, so
the load-bearing assertions moved rather than disappeared. What must hold now:

  - the order tools exist, but `place_order` takes ONLY a ticket_id, so nothing
    here can construct and fire an order in one call
  - the guards stay server-side; this module must not reimplement them
  - the instructions tell the model to confirm out loud before sending

Deliberately NOT `importorskip`. `mcp` is in requirements-dev.txt, so a failure
to import means this module is broken, not absent — and a skip would hide it.

That is not hypothetical, and the example is this file's own history. mcp 2.0
renamed `FastMCP` to `MCPServer` and dropped `mcp.server.fastmcp`. A version of
mcp_server.py written against 1.x imported the old path; because `requirements`
is unpinned, CI installed 2.0 and the import failed. With `importorskip` that
would have skipped silently and the run would have gone green over a server that
could not start. Without it, CI failed on the first push. Keep it that way.
"""

from __future__ import annotations

import inspect
import json

import pytest

import mcp_server


async def tools():
    return await mcp_server.mcp.list_tools()


def schema_of(tool):
    """The SDK has used both `inputSchema` and `input_schema`; tolerate either."""
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")


# --- the surface exists ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_read_tools_are_registered():
    names = {t.name for t in await tools()}
    assert {"get_portfolio", "get_gamma", "get_signals", "list_alerts",
            "create_alert", "delete_alert", "test_alert_delivery"} <= names


@pytest.mark.asyncio
async def test_the_market_data_tools_are_registered():
    names = {t.name for t in await tools()}
    assert {"get_quote", "get_bars", "get_order_book", "get_option_chain",
            "get_research", "run_screener"} <= names


@pytest.mark.asyncio
async def test_the_order_tools_are_registered():
    """Rule 3 reversed: these are supposed to be here now."""
    names = {t.name for t in await tools()}
    assert {"preview_order", "preview_option_order", "place_order",
            "replace_order", "cancel_order"} <= names


@pytest.mark.asyncio
async def test_every_tool_has_a_description():
    """The description is the only thing the model reads before choosing."""
    missing = [t.name for t in await tools() if not (t.description or "").strip()]
    assert not missing, f"tools with no description: {missing}"


# --- the order path stays two-step ----------------------------------------


@pytest.mark.asyncio
async def test_place_order_takes_only_a_ticket():
    """The whole safety property in one assertion.

    If place_order ever grows a symbol or a quantity, a single call can both
    construct and fire an order, and the summary the user confirmed out loud is
    no longer guaranteed to be what gets sent.
    """
    tool = next(t for t in await tools() if t.name == "place_order")
    props = set(schema_of(tool)["properties"])
    assert props == {"ticket_id"}, f"place_order grew parameters: {props - {'ticket_id'}}"


def test_preview_does_not_send():
    """A preview tool that could send would defeat the handshake."""
    src = inspect.getsource(mcp_server.preview_order)
    assert "/api/orders/preview" in src
    assert "/api/orders/place" not in src


@pytest.mark.asyncio
async def test_preview_requires_the_three_things_that_define_an_order():
    tool = next(t for t in await tools() if t.name == "preview_order")
    assert set(schema_of(tool)["required"]) == {"symbol", "side", "quantity"}


@pytest.mark.asyncio
async def test_the_one_shot_tool_says_it_needs_confirmation_turned_off():
    """place_order_now exists, but the model must know it is not the normal path."""
    tool = next(t for t in await tools() if t.name == "place_order_now")
    assert "SIDECAR_ORDER_CONFIRM=0" in (tool.description or "")


def test_the_guards_are_not_reimplemented_here():
    """Caps live in orders.py so every caller gets them. A copy would drift."""
    src = inspect.getsource(mcp_server)
    for leaked in ("MAX_NOTIONAL", "SIDECAR_MAX_QUANTITY", "SYMBOL_ALLOWLIST"):
        assert leaked not in src, f"{leaked} should not be enforced in the MCP layer"


def test_this_server_holds_no_credentials():
    """It is a bridge to sidecar, not a second broker client."""
    src = inspect.getsource(mcp_server)
    for leaked in ("WEBULL_KEY", "WEBULL_SECRET", "ApiClient", "TradeClient"):
        assert leaked not in src, f"MCP server should not touch {leaked}"


# --- instructions ----------------------------------------------------------


def test_instructions_demand_confirmation_before_sending():
    text = (mcp_server.mcp.instructions or "").lower()
    assert "preview_order" in text
    assert "said yes" in text or "confirm" in text


def test_instructions_frame_gamma_as_positioning_not_a_forecast():
    """The failure mode is a level being read back as a price target."""
    text = (mcp_server.mcp.instructions or "").lower()
    assert "not a forecast" in text


def test_instructions_say_alerts_outlive_the_conversation():
    text = (mcp_server.mcp.instructions or "").lower()
    assert "background" in text or "conversation ends" in text


# --- alerts ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_alert_accepts_a_number_or_a_named_level():
    """A model asked for 'when SPY breaks 743' sends a NUMBER.

    Typed as `str` alone this failed schema validation before ever reaching the
    server, which is exactly how it shipped the first time.
    """
    tool = next(t for t in await tools() if t.name == "create_alert")
    rendered = json.dumps(schema_of(tool)["properties"]["level"])
    assert "string" in rendered and "number" in rendered


@pytest.mark.asyncio
async def test_create_alert_requires_the_three_things_that_define_an_alert():
    tool = next(t for t in await tools() if t.name == "create_alert")
    assert set(schema_of(tool)["required"]) == {"symbol", "level", "direction"}


# --- failure reporting -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreachable_sidecar_names_the_url_and_the_likely_cause(monkeypatch):
    """A dead sidecar must be distinguishable from a rejected order.

    The SDK wraps a raising tool in a tool error and hands the message to the
    model, so the message is the whole user-facing surface here — it has to say
    where it tried and what to do about it.
    """
    monkeypatch.setattr(mcp_server, "SIDECAR_URL", "http://127.0.0.1:1")  # nothing listens
    with pytest.raises(Exception) as exc:
        await mcp_server.mcp.call_tool("get_gamma", {"symbol": "SPY"})
    text = str(exc.value)
    assert "127.0.0.1:1" in text, "the failure does not say where it tried"
    assert "run.sh" in text or "SIDECAR_URL" in text, "no hint at the fix"


@pytest.mark.asyncio
async def test_a_guard_rejection_reads_as_an_answer_not_a_transport_error(monkeypatch):
    """A capped order is a real answer the model should read aloud verbatim."""
    class Resp:
        status_code = 400
        content = b"{}"

        def json(self):
            return {"detail": "order notional ~$75,600.00 exceeds SIDECAR_MAX_NOTIONAL"}

    monkeypatch.setattr(mcp_server, "_call",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError(Resp().json()["detail"])))
    with pytest.raises(Exception) as exc:
        await mcp_server.mcp.call_tool(
            "preview_order", {"symbol": "AAPL", "side": "BUY", "quantity": 9000})
    assert "MAX_NOTIONAL" in str(exc.value)
