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

    # The MCP notional cap is now portfolio-aware (min of the absolute
    # ceiling and pct x NLV) and fails CLOSED when NLV cannot be read, so
    # every test needs an account state to size against. These defaults put
    # the portfolio cap ($8000) well above the ceiling ($2000) so the
    # ceiling is the binding constraint and the pre-existing expectations
    # in this file still describe the behaviour under test.
    monkeypatch.setenv("MCP_MAX_NOTIONAL_PCT", "1.0")
    _stub_account(monkeypatch, net_liquidation=8000.0)


def _stub_account(monkeypatch, net_liquidation=8000.0, available=True, stale=False):
    """Point `_effective_notional_cap()` at a synthetic account."""
    import trading_mcp.vesper_tools as vt

    monkeypatch.setattr(
        vt,
        "fetch_account_state",
        lambda: {
            "available": available,
            "stale": stale,
            "fetch_error": None if available else "stubbed outage",
            "net_liquidation": net_liquidation,
        },
    )


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


def test_submit_manual_proposal_short_option_sized_off_strike(monkeypatch):
    """M8-20: Short option sell-to-open requires strike and sizes notional off strike * 100 * qty."""
    # 1. Short option missing strike is refused
    # Lift the MCP cap clear so the GUARD's cap is the binding constraint --
    # that is the layer this test pins. The MCP cap's own strike-vs-premium
    # behaviour is covered by
    # test_short_option_cap_is_sized_off_strike_not_premium.
    monkeypatch.setenv("MCP_MAX_NOTIONAL", "1000000.0")
    _stub_account(monkeypatch, net_liquidation=1000000.0)

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

    # Lift the MCP cap clear so the GUARD's cap is the binding constraint.
    monkeypatch.setenv("MCP_MAX_NOTIONAL", "1000000.0")

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
    assert "portfolio-aware MCP cap" in res_notional["reason"]

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


def test_two_step_path_cannot_bypass_mcp_notional_cap(monkeypatch):
    """REGRESSION (M8-24). The MCP notional cap used to live only in
    `place_order`, so the two-step tool path -- `submit_manual_proposal_tool`
    then `place_from_ticket_tool` -- reached the broker bounded by nothing
    but the guard's own, much larger, `VESPER_MAX_NOTIONAL`. That is a real
    bypass and it would have gone live the moment `register_order_tools` was
    wired into `server.py`.

    Here the order is inside the guard's $5000 cap but outside the MCP's
    $2000 one. Staging must refuse it, which means there is no ticket for
    step two to fire."""
    mock_place_fn = MagicMock(return_value={"status": "FILLED"})

    res = ot.submit_manual_proposal(
        ticker="AAPL", side="BUY", quantity=20, limit_price=150.0,  # $3000
    )
    assert res["available"] is False, "staging must enforce the MCP cap"
    assert res["rejected"] is True
    assert "portfolio-aware MCP cap" in res["reason"]
    assert "ticket_id" not in res

    # And with no ticket, the second step has nothing to fire.
    assert not mock_place_fn.called


def test_notional_cap_is_portfolio_aware(monkeypatch):
    """The operative cap scales with the book, not with a flat dollar
    constant. On a small account the portfolio term binds well below the
    absolute ceiling."""
    _stub_account(monkeypatch, net_liquidation=1513.34)
    monkeypatch.setenv("MCP_MAX_NOTIONAL_PCT", "0.25")

    cap, detail = ot._effective_notional_cap()
    assert cap == pytest.approx(378.335)
    assert detail["binding"] == "portfolio"
    assert detail["ceiling"] == 2000.0

    # An order the flat $2000 ceiling would have waved through is refused.
    res = ot.submit_manual_proposal(
        ticker="SOFI", side="BUY", quantity=30, limit_price=18.43,  # ~$553
    )
    assert res["rejected"] is True
    assert "portfolio-aware MCP cap" in res["reason"]


def test_notional_cap_fails_closed_when_account_unavailable(monkeypatch):
    """A cap computed from an unknown book is not a cap. When NLV cannot be
    read the cap is 0 and every opening order is refused -- it must never
    silently fall back to the flat ceiling, which is the exact failure this
    replaced."""
    _stub_account(monkeypatch, net_liquidation=0.0, available=False)

    cap, detail = ot._effective_notional_cap()
    assert cap == 0.0
    assert "account state unavailable" in detail["reason"]

    res = ot.submit_manual_proposal(
        ticker="AAPL", side="BUY", quantity=1, limit_price=1.0,
    )
    assert res["rejected"] is True

    res_place = ot.place_order(
        ticker="AAPL", side="BUY", quantity=1, limit_price=1.0,
        place_fn=MagicMock(),
    )
    assert res_place["rejected"] is True


def test_closing_orders_are_not_blocked_by_the_cap(monkeypatch):
    """Exposure-reducing orders skip the notional cap. The server's stated
    exposure rule is that anything which cannot increase exposure is
    permitted; a cap that blocks you from closing a position you already
    hold is a cap that traps risk on a bad day."""
    _stub_account(monkeypatch, net_liquidation=1513.34)
    monkeypatch.setenv("MCP_MAX_NOTIONAL_PCT", "0.25")

    res = ot.submit_manual_proposal(
        ticker="SOFI", side="SELL", quantity=30, limit_price=18.43,  # ~$553
        is_closing=True,
    )
    assert res.get("staged") is True, res.get("reason")


def test_short_option_cap_is_sized_off_strike_not_premium(monkeypatch):
    """The strike-vs-premium rule that `execution_guard` enforces has to
    hold on the MCP path too. A cash-secured put priced at $0.50 premium
    commits `strike x 100 x qty` on assignment; sizing the cap off the
    premium is how a five-figure risk sails past a three-figure cap."""
    _stub_account(monkeypatch, net_liquidation=1513.34)
    monkeypatch.setenv("MCP_MAX_NOTIONAL_PCT", "0.25")

    notional, err = ot._notional_for(
        quantity=1, limit_price=0.50, asset_type="OPTION",
        side="SELL", strike=190.0, is_closing=False,
    )
    assert err is None
    assert notional == 19000.0, "must be strike x 100, not premium x 100"

    res = ot.submit_manual_proposal(
        ticker="AAPL", side="SELL", quantity=1, limit_price=0.50,
        asset_type="OPTION", strike=190.0,
    )
    assert res["rejected"] is True
