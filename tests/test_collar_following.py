"""Unit tests for Collar-Following Cash-Secured Put Playbook."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from vesper.nodes.playbooks import (
    _extract_fund_put_hedges,
    _fetch_live_option_quote,
    playbooks_node,
)
from vesper.state import TradingState, OrderProposal
from vesper.execution_guard import ExecutionGuard, GuardError


def test_extract_fund_put_hedges_various_schemas():
    """Verify extraction of (underlying, strike) from different TickerTrace income response structures."""
    sample_data = {
        "fund": "ULTY",
        "options": [
            {"symbol": "TSLA_240920P00210000", "option_type": "PUT", "underlying": "TSLA", "strike": 210.0},
            {"symbol": "NVDA_240920C00125000", "option_type": "CALL", "underlying": "NVDA", "strike": 125.0},
            {"description": "AMD 140 PUT", "type": "PUT", "underlying_symbol": "AMD", "strike": 140.0},
            {"description": "COIN 200 P", "type": "OPTION", "strike": 200.0},  # derived from description
        ],
    }

    hedges = _extract_fund_put_hedges(sample_data, default_ticker="ULTY")
    assert ("TSLA", 210.0) in hedges
    assert ("AMD", 140.0) in hedges
    assert ("COIN", 200.0) in hedges
    # CALL must be excluded
    assert ("NVDA", 125.0) not in hedges


def test_extract_fund_put_hedges_empty_and_deduplication():
    """Verify empty handling and deduplication."""
    empty_res = _extract_fund_put_hedges({}, default_ticker="QQQI")
    assert empty_res == []

    dup_data = {
        "holdings": [
            {"option_type": "PUT", "underlying": "QQQ", "strike": 480.0},
            {"option_type": "PUT", "underlying": "QQQ", "strike": 480.0},
        ]
    }
    hedges = _extract_fund_put_hedges(dup_data, default_ticker="QQQI")
    assert len(hedges) == 1
    assert hedges[0] == ("QQQ", 480.0)


def test_fetch_live_option_quote_unconfigured():
    """When Webull is not configured, _fetch_live_option_quote returns None (no placeholder)."""
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock()
        mock_wb.configured = False
        mock_wb_cls.return_value = mock_wb

        res = _fetch_live_option_quote("SPY", 550.0, "PUT")
        assert res is None


def test_fetch_live_option_quote_success():
    """When Webull market data returns valid quote, returns mid/last price."""
    with patch("wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock()
        mock_wb.configured = True
        mock_wb_cls.return_value = mock_wb

        with patch("md.Market") as mock_mkt_cls:
            mock_mkt = MagicMock()
            mock_mkt.option_chain.return_value = [
                {"symbol": "SPY240920P00550000", "strike_price": 550.0}
            ]
            mock_mkt.option_snapshot.return_value = {
                "SPY240920P00550000": {"bid": 2.40, "ask": 2.60, "last": 2.50}
            }
            mock_mkt_cls.return_value = mock_mkt

            price = _fetch_live_option_quote("SPY", 550.0, "PUT")
            assert price == 2.50


@pytest.mark.asyncio
async def test_collar_following_playbook_inert_when_env_unset(monkeypatch):
    """When VESPER_COLLAR_FOLLOW_FUNDS is empty, playbook produces no proposals."""
    monkeypatch.setenv("VESPER_COLLAR_FOLLOW_FUNDS", "")

    state: TradingState = {
        "session_id": "test-inert",
        "selected_playbook": "collar_following",
        "candidates": [],
        "technicals": {},
        "proposals": [],
        "risk_assessments": {},
        "needs_human_approval": False,
        "audit_trail": [],
    }

    res = await playbooks_node(state)
    assert res["proposals"] == []
    assert res["needs_human_approval"] is False


@pytest.mark.asyncio
async def test_collar_following_playbook_drafts_and_skips_correctly(monkeypatch):
    """Verify proposal drafting with real quotes and skipping when quote is unavailable."""
    monkeypatch.setenv("VESPER_COLLAR_FOLLOW_FUNDS", "ULTY,QQQI")

    ulty_data = {
        "options": [
            {"option_type": "PUT", "underlying": "TSLA", "strike": 210.0},
            {"option_type": "PUT", "underlying": "NVDA", "strike": 115.0},
        ]
    }
    qqqi_data = {
        "holdings": [
            {"option_type": "PUT", "underlying": "QQQ", "strike": 470.0},
        ]
    }

    def mock_fetch_income(fund: str):
        if fund == "ULTY":
            return ulty_data
        if fund == "QQQI":
            return qqqi_data
        return None

    def mock_fetch_quote(underlying: str, strike: float, option_type: str):
        if underlying == "TSLA" and strike == 210.0:
            return 3.45
        if underlying == "QQQ" and strike == 470.0:
            return 2.15
        # NVDA has no live quote
        return None

    state: TradingState = {
        "session_id": "test-collar",
        "selected_playbook": "collar_following",
        "candidates": [],
        "technicals": {},
        "proposals": [],
        "risk_assessments": {},
        "needs_human_approval": False,
        "audit_trail": [],
    }

    with patch("vesper.nodes.playbooks._fetch_income_fund_detail", side_effect=mock_fetch_income):
        with patch("vesper.nodes.playbooks._fetch_live_option_quote", side_effect=mock_fetch_quote):
            res = await playbooks_node(state)

    proposals: list[OrderProposal] = res["proposals"]
    assert len(proposals) == 2
    assert res["needs_human_approval"] is True

    # Check TSLA Collar CSP
    tsla_prop = next(p for p in proposals if p.ticker == "TSLA")
    assert tsla_prop.side == "SELL"
    assert tsla_prop.asset_type == "OPTION"
    assert tsla_prop.option_type == "put"
    assert tsla_prop.strike == 210.0
    assert tsla_prop.quantity == 1
    assert tsla_prop.limit_price == 3.45
    # Assignment capital = 210 * 100 * 1 = $21,000 (NOT premium $345)
    assert tsla_prop.estimated_cost == 21000.0
    assert tsla_prop.max_risk == 21000.0

    # Check QQQ Collar CSP
    qqq_prop = next(p for p in proposals if p.ticker == "QQQ")
    assert qqq_prop.side == "SELL"
    assert qqq_prop.strike == 470.0
    assert qqq_prop.limit_price == 2.15
    assert qqq_prop.estimated_cost == 47000.0
    assert qqq_prop.max_risk == 47000.0

    # Check that NVDA was skipped in audit notes
    notes = res["audit_trail"][0]["notes"]
    assert any("Skipped Collar CSP for NVDA strike $115.00" in n and "no live option quote" in n for n in notes)


@pytest.mark.asyncio
async def test_collar_proposal_flows_through_execution_guard(monkeypatch):
    """Verify that drafted Collar CSP proposal is checked against strike-based notional cap in ExecutionGuard."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "50000")  # $50,000 cap

    guard = ExecutionGuard()

    # A $210 strike CSP requires $21,000 capital (passes $50k cap)
    payload_pass = {
        "symbol": "TSLA",
        "side": "SELL",
        "quantity": 1,
        "limit_price": 3.45,
        "asset_type": "OPTION",
        "strike": 210.0,
    }
    ticket = guard.preview("prop-collar-pass", payload_pass)
    assert ticket.id is not None

    # A $600 strike CSP requires $60,000 capital (exceeds $50k cap -> rejected)
    payload_fail = {
        "symbol": "NVDA",
        "side": "SELL",
        "quantity": 1,
        "limit_price": 4.50,
        "asset_type": "OPTION",
        "strike": 600.0,
    }
    with pytest.raises(GuardError, match="exceeds VESPER_MAX_NOTIONAL"):
        guard.preview("prop-collar-fail", payload_fail)
