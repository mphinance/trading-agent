"""Unit tests for Premium-Recycling Free Share Engine (Task 2)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from core.paper_ledger import (
    _load_ledger,
    _save_ledger,
    get_paper_summary,
    get_unswept_premium,
    mark_premium_swept,
    record_paper_fill,
)
from vesper.nodes.playbooks import playbooks_node
from vesper.state import TradingState, OrderProposal, ExecutionResult


@pytest.fixture
def clean_paper_ledger(tmp_path, monkeypatch):
    """Isolate paper ledger storage."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = data_dir / "paper_ledger.json"
    monkeypatch.setattr("core.paper_ledger._DATA_DIR", data_dir)
    monkeypatch.setattr("core.paper_ledger._LEDGER_PATH", ledger_path)
    return ledger_path


def _make_state(selected_playbook: str = "recycle") -> TradingState:
    return {
        "session_id": "test-recycle-sess",
        "selected_playbook": selected_playbook,
        "candidates": [],
        "technicals": {},
        "options_audits": {},
        "proposals": [],
        "risk_assessments": {},
        "needs_human_approval": False,
        "audit_trail": [],
    }


@pytest.mark.asyncio
async def test_premium_recycling_insufficient_pnl_drafts_nothing(clean_paper_ledger, monkeypatch):
    """When unswept realized PnL is below the 100-share block cost, no proposal is drafted."""
    monkeypatch.setenv("VESPER_PREMIUM_RECYCLE_TICKER", "SGOV")

    # Set up paper ledger with only $500 realized PnL
    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 500.0
    ledger["account"]["swept_premium"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=100.50):
        res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    assert res["needs_human_approval"] is False
    notes = res["audit_trail"][0]["notes"]
    assert any("below 100-share threshold for SGOV" in n for n in notes)


@pytest.mark.asyncio
async def test_premium_recycling_sufficient_pnl_drafts_100_share_buy(clean_paper_ledger, monkeypatch):
    """When unswept realized PnL >= 100-share block cost, drafts 100 shares BUY."""
    monkeypatch.setenv("VESPER_PREMIUM_RECYCLE_TICKER", "SGOV")

    # Set up paper ledger with $15,000 realized PnL
    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 15000.0
    ledger["account"]["swept_premium"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=100.50):
        res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    p: OrderProposal = props[0]
    assert p.ticker == "SGOV"
    assert p.asset_type == "EQUITY"
    assert p.side == "BUY"
    assert p.quantity == 100
    assert p.limit_price == 100.50
    assert p.estimated_cost == 10050.0
    assert p.max_risk == 10050.0

    # Drafting alone MUST NOT mark the premium as swept prematurely
    summary = get_paper_summary()
    assert summary["swept_premium"] == 0.0
    assert summary["unswept_premium"] == 11250.0


@pytest.mark.asyncio
async def test_premium_recycling_rejected_proposal_does_not_spend_premium(clean_paper_ledger, monkeypatch):
    """If a drafted premium recycling proposal is rejected, premium remains unswept."""
    monkeypatch.setenv("VESPER_PREMIUM_RECYCLE_TICKER", "SGOV")

    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 15000.0
    ledger["account"]["swept_premium"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=100.50):
        res = await playbooks_node(state)

    prop = res["proposals"][0]

    # Simulate human rejection (no execution fill recorded)
    # The ledger swept_premium must remain 0.0
    assert get_unswept_premium() == 11250.0


@pytest.mark.asyncio
async def test_premium_recycling_executed_proposal_marks_swept_and_prevents_double_draft(
    clean_paper_ledger, monkeypatch
):
    """When a recycling proposal is executed, its cost is marked swept in the ledger,
    preventing duplicate proposals on the subsequent run."""
    monkeypatch.setenv("VESPER_PREMIUM_RECYCLE_TICKER", "SGOV")

    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 15000.0
    ledger["account"]["swept_premium"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=100.50):
        res1 = await playbooks_node(state)

    prop = res1["proposals"][0]
    assert prop.id.startswith("prop-recycle-")

    # Simulate fill execution in paper ledger (Module 7 executor)
    sim_res = ExecutionResult(
        order_proposal_id=prop.id,
        ticker=prop.ticker,
        status="DRY_RUN_SIMULATED",
        filled_quantity=prop.quantity,
        filled_price=prop.limit_price,
    )
    record_paper_fill(proposal=prop, result=sim_res)

    # Verify swept premium was updated in ledger
    summary = get_paper_summary()
    assert summary["swept_premium"] == 10050.0
    assert summary["unswept_premium"] == 1200.0  # 0.75 * 15000 - 10050 = 11250 - 10050

    # On the second run, unswept PnL ($4,950) is now below $10,050 threshold
    state2 = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=100.50):
        res2 = await playbooks_node(state2)

    assert len(res2["proposals"]) == 0
    assert any("below 100-share threshold for SGOV ($10,050.00)" in n for n in res2["audit_trail"][0]["notes"])


@pytest.mark.asyncio
async def test_premium_recycling_skips_when_quote_unavailable(clean_paper_ledger, monkeypatch):
    """Verify proposal is skipped if live quote for the recycling stabilizer is unavailable."""
    monkeypatch.setenv("VESPER_PREMIUM_RECYCLE_TICKER", "SGOV")

    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 25000.0
    ledger["account"]["swept_premium"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=None):
        res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    assert any("no live quote available" in n for n in res["audit_trail"][0]["notes"])
