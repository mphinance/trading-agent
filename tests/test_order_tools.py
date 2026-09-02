"""Tests for order tools and AST exposure pin under Amendment A4 (M8-20..M8-23)."""

from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import trading_mcp.order_tools as ot
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, AuthContext, run_auth_checks
from vesper.execution_guard import GuardError, Ticket, guard


@pytest.fixture(autouse=True)
def clean_mcp_order_env(tmp_path, monkeypatch):
    """Isolate order rate limit file and default guard config."""
    rate_file = tmp_path / "mcp_daily_order_count.json"
    monkeypatch.setattr(ot, "_RATE_LIMIT_FILE", rate_file)

    chain_file = tmp_path / "audit_chain.jsonl"
    import core.audit_chain as ac
    monkeypatch.setattr(ac, "_CHAIN_PATH", chain_file)

    # Enable trading for test by default
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "5000.0")
    monkeypatch.setenv("VESPER_MAX_QUANTITY", "50.0")
    monkeypatch.setenv("MCP_MAX_NOTIONAL", "2000.0")
    monkeypatch.setenv("MCP_MAX_DAILY_ORDERS", "3")


def test_submit_manual_proposal_stages_ticket_and_never_places(monkeypatch):
    """M8-20: submit_manual_proposal stages a ticket carrying payload hash and returns it."""
    res = ot.submit_manual_proposal(
        ticker="AAPL",
        side="BUY",
        quantity=10,
        limit_price=150.0,
        order_type="LIMIT",
    )
    assert res["available"] is True
    assert res["staged"] is True
    assert "ticket_id" in res
    assert "digest" in res
    ticket_id = res["ticket_id"]

    # Confirm ticket exists in execution_guard._tickets
    ticket = guard._tickets.get(ticket_id)
    assert ticket is not None
    assert not ticket.used
    assert res["payload"]["ticker"] == "AAPL"
    assert ot._STAGED_PAYLOADS[ticket_id]["ticker"] == "AAPL"


def test_submit_manual_proposal_short_option_sized_off_strike():
    """M8-20: Short option sell-to-open requires strike and sizes notional off strike * 100 * qty."""
    # 1. Short option missing strike is refused
    res_no_strike = ot.submit_manual_proposal(
        ticker="SPY",
        side="SELL",
        quantity=1,
        limit_price=2.50,
        asset_type="OPTION",
        strike=None,
        is_closing=False,
    )
    assert res_no_strike["available"] is False
    assert res_no_strike["rejected"] is True

    # 2. Short option with strike 500 has notional = 1 * 500 * 100 = $50,000, exceeding VESPER_MAX_NOTIONAL ($5000)
    res_sized = ot.submit_manual_proposal(
        ticker="SPY",
        side="SELL",
        quantity=1,
        limit_price=2.50,
        asset_type="OPTION",
        strike=500.0,
        is_closing=False,
    )
    assert res_sized["available"] is False
    assert res_sized["rejected"] is True
    assert "VESPER_MAX_NOTIONAL" in res_sized["reason"]


def test_submit_manual_proposal_rejections_halt_and_notional(tmp_path, monkeypatch):
    """M8-20: Explicit rejection reasons on halt or notional cap."""
    import core.halt as ch
    halt_file = tmp_path / "halt_state.json"
    monkeypatch.setattr(ch, "_HALT_STATE_PATH", halt_file)

    # 1. Notional cap rejection ($150 * 50 = $7500 > $5000)
    res_notional = ot.submit_manual_proposal(
        ticker="AAPL",
        side="BUY",
        quantity=50,
        limit_price=150.0,
    )
    assert res_notional["available"] is False
    assert res_notional["rejected"] is True
    assert "VESPER_MAX_NOTIONAL" in res_notional["reason"]

    # 2. Emergency halt rejection
    ch.halt(reason="Halt for manual proposal test", source="unit_test")
    res_halt = ot.submit_manual_proposal(
        ticker="AAPL",
        side="BUY",
        quantity=5,
        limit_price=150.0,
    )
    assert res_halt["available"] is False
    assert res_halt["rejected"] is True
    assert "HALTED" in res_halt["reason"]


def test_place_from_ticket_single_use_and_no_payload_input():
    """M8-21: place_from_ticket fires previously staged ticket, refuses reuse or unknown tickets."""
    # 1. Stage ticket
    preview_res = ot.submit_manual_proposal(
        ticker="NVDA",
        side="BUY",
        quantity=5,
        limit_price=100.0,
    )
    assert preview_res["staged"] is True
    ticket_id = preview_res["ticket_id"]

    mock_place_fn = MagicMock(return_value={"status": "FILLED", "id": "test-fill-1"})

    # 2. Fire ticket
    res = ot.place_from_ticket(ticket_id=ticket_id, place_fn=mock_place_fn)
    assert res["available"] is True
    assert res["placed"] is True
    assert mock_place_fn.called

    # 3. Second call on same ticket is refused (single-use)
    res_reuse = ot.place_from_ticket(ticket_id=ticket_id, place_fn=mock_place_fn)
    assert res_reuse["available"] is False
    assert res_reuse["rejected"] is True

    # 4. Forged or unknown ticket refused
    res_fake = ot.place_from_ticket(ticket_id="fake-ticket-12345", place_fn=mock_place_fn)
    assert res_fake["available"] is False
    assert res_fake["rejected"] is True


def test_place_order_mcp_specific_limits(monkeypatch, tmp_path):
    """M8-22: place_order enforces MCP_MAX_NOTIONAL and MCP_MAX_DAILY_ORDERS."""
    mock_place_fn = MagicMock(return_value={"status": "FILLED"})

    # 1. Exceeds MCP_MAX_NOTIONAL ($2000): 15 * $150 = $2250 > $2000
    res_notional = ot.place_order(
        ticker="AAPL",
        side="BUY",
        quantity=15,
        limit_price=150.0,
        place_fn=mock_place_fn,
    )
    assert res_notional["available"] is False
    assert res_notional["rejected"] is True
    assert "MCP_MAX_NOTIONAL" in res_notional["reason"]

    # 2. Valid orders up to MCP_MAX_DAILY_ORDERS (limit=3)
    res1 = ot.place_order(ticker="AAPL", side="BUY", quantity=1, limit_price=150.0, place_fn=mock_place_fn)
    assert res1["placed"] is True
    res2 = ot.place_order(ticker="AAPL", side="BUY", quantity=1, limit_price=150.0, place_fn=mock_place_fn)
    assert res2["placed"] is True
    res3 = ot.place_order(ticker="AAPL", side="BUY", quantity=1, limit_price=150.0, place_fn=mock_place_fn)
    assert res3["placed"] is True

    # 3. 4th order hits daily limit (limit=3)
    res4 = ot.place_order(ticker="AAPL", side="BUY", quantity=1, limit_price=150.0, place_fn=mock_place_fn)
    assert res4["available"] is False
    assert res4["rejected"] is True
    assert "daily order limit" in res4["reason"].lower()


@pytest.mark.asyncio
async def test_order_tools_require_trade_scope():
    """M8-23: Order tools require 'trade' scope; refused for read-only tokens."""
    m = FastMCP("test-order-scopes")
    ot.register_order_tools(m)

    read_tok = AccessToken(token="read-tok", client_id="c1", scopes=["read", "safe-write"])
    trade_tok = AccessToken(token="trade-tok", client_id="c2", scopes=["read", "trade"])

    order_tools = ["submit_manual_proposal_tool", "place_from_ticket_tool", "place_order_tool"]
    for tool_name in order_tools:
        comp = m._local_provider._components[f"tool:{tool_name}@"]
        assert comp.auth is not None

        # Read-only token fails auth check
        ctx_read = AuthContext(token=read_tok, component=comp)
        assert not await run_auth_checks(comp.auth, ctx_read)

        # Trade-scoped token passes auth check
        ctx_trade = AuthContext(token=trade_tok, component=comp)
        assert await run_auth_checks(comp.auth, ctx_trade)


def test_order_tools_ast_exposure_pin():
    """M8-23: Confirm submit_decision and resume are absent from order_tools.py."""
    source = Path(ot.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "resume" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name != "resume"

        if isinstance(node, ast.Attribute):
            assert node.attr not in ("submit_decision", "resume")
