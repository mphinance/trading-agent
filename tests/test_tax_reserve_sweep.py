"""Unit tests for the 25% Realized-P&L Tax Reserve Sweep (to $SGOV).

Mirrors tests/test_premium_recycling.py's structure and fixture pattern.
The free-share engine (Premium Recycling) and the Tax Reserve Sweep are two
independent pools carved out of the same `realized_pnl` number (75% / 25%
respectively) -- see paper_ledger.get_paper_summary and the "TAX RESERVE
SWEEP" section in vesper/nodes/playbooks.py.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from vesper.paper_ledger import (
    _load_ledger,
    _save_ledger,
    get_paper_summary,
    get_unswept_premium,
    get_unswept_tax_reserve,
    mark_tax_reserve_swept,
    record_paper_fill,
)
from vesper.nodes.playbooks import playbooks_node
from vesper.state import TradingState, OrderProposal, ExecutionResult


@pytest.fixture
def clean_paper_ledger(tmp_path, monkeypatch):
    """Isolate paper ledger storage.

    Redundant with the global `_isolated_vesper_state` autouse fixture in
    tests/conftest.py (which already redirects vesper.paper_ledger's
    _DATA_DIR/_LEDGER_PATH), kept for parity with test_premium_recycling.py's
    existing convention and to keep this file self-contained if that global
    fixture's module list ever changes.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = data_dir / "paper_ledger.json"
    monkeypatch.setattr("vesper.paper_ledger._DATA_DIR", data_dir)
    monkeypatch.setattr("vesper.paper_ledger._LEDGER_PATH", ledger_path)
    return ledger_path


def _make_state(selected_playbook: str = "tax_reserve") -> TradingState:
    return {
        "session_id": "test-taxsweep-sess",
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
async def test_tax_reserve_insufficient_pnl_drafts_nothing(clean_paper_ledger, monkeypatch):
    """When unswept 25% tax reserve is below the price of one share, no proposal is drafted."""
    monkeypatch.setenv("VESPER_TAX_RESERVE_TICKER", "SGOV")

    # realized_pnl=50.0 -> 25% pool = 12.50, well below a $97.33 share.
    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 50.0
    ledger["account"]["tax_reserve_swept"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=97.33):
        res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    assert res["needs_human_approval"] is False
    notes = res["audit_trail"][0]["notes"]
    assert any("below price of one share of SGOV" in n for n in notes)


@pytest.mark.asyncio
async def test_tax_reserve_sufficient_pnl_drafts_whole_share_buy(clean_paper_ledger, monkeypatch):
    """When unswept 25% tax reserve covers >= 1 share, drafts a WHOLE-SHARE buy
    (not a 100-share block) -- the quotient is deliberately non-round to prove
    it isn't silently landing on 100."""
    monkeypatch.setenv("VESPER_TAX_RESERVE_TICKER", "SGOV")

    # realized_pnl=15000.0 -> 25% pool = 3750.00. At $97.33/share:
    # 3750.00 // 97.33 = 38 shares (not a round multiple of 100).
    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 15000.0
    ledger["account"]["tax_reserve_swept"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=97.33):
        res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    p: OrderProposal = props[0]
    assert p.id.startswith("prop-taxsweep-")
    assert p.ticker == "SGOV"
    assert p.asset_type == "EQUITY"
    assert p.side == "BUY"
    assert p.quantity == 38
    assert p.quantity != 100  # not a 100-share block -- whole-share sizing
    assert p.limit_price == 97.33
    assert p.estimated_cost == round(97.33 * 38, 2)
    assert p.max_risk == round(97.33 * 38, 2)

    # Drafting alone MUST NOT mark the tax reserve as swept prematurely.
    summary = get_paper_summary()
    assert summary["tax_reserve_swept"] == 0.0
    assert summary["unswept_tax_reserve"] == 3750.0


@pytest.mark.asyncio
async def test_tax_reserve_rejected_proposal_does_not_spend_reserve(clean_paper_ledger, monkeypatch):
    """If a drafted tax-reserve-sweep proposal is rejected (never filled), the
    reserve remains fully unswept."""
    monkeypatch.setenv("VESPER_TAX_RESERVE_TICKER", "SGOV")

    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 15000.0
    ledger["account"]["tax_reserve_swept"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=97.33):
        res = await playbooks_node(state)

    assert len(res["proposals"]) == 1

    # Simulate human rejection (no execution fill recorded).
    assert get_unswept_tax_reserve() == 3750.0


@pytest.mark.asyncio
async def test_tax_reserve_executed_proposal_marks_swept_and_reduces_next_draft(
    clean_paper_ledger, monkeypatch
):
    """When a tax-reserve-sweep proposal is executed, its cost is marked swept
    (in tax_reserve_swept, NOT swept_premium), and the next run's unswept pool
    shrinks accordingly."""
    monkeypatch.setenv("VESPER_TAX_RESERVE_TICKER", "SGOV")

    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 15000.0
    ledger["account"]["tax_reserve_swept"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=97.33):
        res1 = await playbooks_node(state)

    prop = res1["proposals"][0]
    assert prop.id.startswith("prop-taxsweep-")
    assert prop.quantity == 38

    sim_res = ExecutionResult(
        order_proposal_id=prop.id,
        ticker=prop.ticker,
        status="DRY_RUN_SIMULATED",
        filled_quantity=prop.quantity,
        filled_price=prop.limit_price,
    )
    record_paper_fill(proposal=prop, result=sim_res)

    filled_cost = round(97.33 * 38, 2)
    summary = get_paper_summary()
    assert summary["tax_reserve_swept"] == filled_cost
    assert summary["swept_premium"] == 0.0  # the OTHER pool untouched
    assert summary["unswept_tax_reserve"] == round(3750.0 - filled_cost, 2)

    # Second run drafts a smaller (or zero) proposal against the shrunken pool.
    state2 = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=97.33):
        res2 = await playbooks_node(state2)

    if res2["proposals"]:
        assert res2["proposals"][0].quantity < 38
    # Either way, no double-draft of the original 38-share amount.
    assert not res2["proposals"] or res2["proposals"][0].quantity != 38


def test_load_ledger_migrates_missing_tax_reserve_swept(clean_paper_ledger):
    """A pre-existing ledger (e.g. one written before this feature landed) that
    has swept_premium but no tax_reserve_swept key must be migrated in-place
    by _load_ledger(), not raise or silently omit the field."""
    import json

    clean_paper_ledger.parent.mkdir(parents=True, exist_ok=True)
    clean_paper_ledger.write_text(json.dumps({
        "account": {
            "initial_cash": 100_000.0,
            "cash": 100_000.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 15000.0,
            "swept_premium": 0.0,
            # tax_reserve_swept deliberately absent -- simulates pre-feature data.
        },
        "fills": [],
        "closed_trades": [],
    }))

    loaded = _load_ledger()
    assert loaded["account"]["tax_reserve_swept"] == 0.0

    summary = get_paper_summary()
    assert summary["tax_reserve_swept"] == 0.0
    assert summary["unswept_tax_reserve"] == 3750.0


def test_mark_tax_reserve_swept_accumulates(clean_paper_ledger):
    """mark_tax_reserve_swept adds to (not replaces) the running total, mirroring
    mark_premium_swept's accumulation behavior."""
    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 15000.0
    ledger["account"]["tax_reserve_swept"] = 0.0
    _save_ledger(ledger)

    first = mark_tax_reserve_swept(1000.0)
    assert first == 1000.0
    second = mark_tax_reserve_swept(500.0)
    assert second == 1500.0

    summary = get_paper_summary()
    assert summary["tax_reserve_swept"] == 1500.0
    assert summary["unswept_tax_reserve"] == round(0.25 * 15000.0 - 1500.0, 2)


@pytest.mark.asyncio
async def test_tax_reserve_skips_when_quote_unavailable(clean_paper_ledger, monkeypatch):
    """Verify the proposal is skipped (never fabricated) if a live quote for
    the tax-reserve ticker is unavailable."""
    monkeypatch.setenv("VESPER_TAX_RESERVE_TICKER", "SGOV")

    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = 25000.0
    ledger["account"]["tax_reserve_swept"] = 0.0
    _save_ledger(ledger)

    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=None):
        res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    assert any("no live quote available" in n for n in res["audit_trail"][0]["notes"])


@pytest.mark.asyncio
async def test_free_share_and_tax_reserve_pools_are_independent(clean_paper_ledger, monkeypatch):
    """The 75% free-share pool and the 25% tax-reserve pool must never double-
    count: filling one must not move the other, and neither pool's
    swept+unswept total may exceed its own share of realized_pnl."""
    monkeypatch.setenv("VESPER_PREMIUM_RECYCLE_TICKER", "SGOV")
    monkeypatch.setenv("VESPER_TAX_RESERVE_TICKER", "SGOV")

    realized_pnl = 15000.0
    ledger = _load_ledger()
    ledger["account"]["realized_pnl"] = realized_pnl
    ledger["account"]["swept_premium"] = 0.0
    ledger["account"]["tax_reserve_swept"] = 0.0
    _save_ledger(ledger)

    recycle_prop = OrderProposal(
        id="prop-recycle-aaaaaa",
        ticker="SGOV",
        asset_type="EQUITY",
        side="BUY",
        order_type="LIMIT",
        quantity=50,
        limit_price=100.0,
        estimated_cost=5000.0,
        max_risk=5000.0,
    )
    recycle_fill = ExecutionResult(
        order_proposal_id=recycle_prop.id,
        ticker="SGOV",
        status="DRY_RUN_SIMULATED",
        filled_quantity=50,
        filled_price=100.0,
    )
    record_paper_fill(proposal=recycle_prop, result=recycle_fill)

    taxsweep_prop = OrderProposal(
        id="prop-taxsweep-bbbbbb",
        ticker="SGOV",
        asset_type="EQUITY",
        side="BUY",
        order_type="LIMIT",
        quantity=10,
        limit_price=100.0,
        estimated_cost=1000.0,
        max_risk=1000.0,
    )
    taxsweep_fill = ExecutionResult(
        order_proposal_id=taxsweep_prop.id,
        ticker="SGOV",
        status="DRY_RUN_SIMULATED",
        filled_quantity=10,
        filled_price=100.0,
    )
    record_paper_fill(proposal=taxsweep_prop, result=taxsweep_fill)

    summary = get_paper_summary()

    # Each pool tracks its own dollars, unaffected by the other's fill.
    assert summary["swept_premium"] == 5000.0
    assert summary["tax_reserve_swept"] == 1000.0
    assert summary["unswept_premium"] == round(0.75 * realized_pnl - 5000.0, 2)
    assert summary["unswept_tax_reserve"] == round(0.25 * realized_pnl - 1000.0, 2)

    # Neither pool's total ever implies more than its allotted share of
    # realized_pnl was drawn from.
    assert (summary["swept_premium"] + summary["unswept_premium"]) == pytest.approx(
        0.75 * realized_pnl
    )
    assert (summary["tax_reserve_swept"] + summary["unswept_tax_reserve"]) == pytest.approx(
        0.25 * realized_pnl
    )

    # get_unswept_premium()/get_unswept_tax_reserve() agree with the summary.
    assert get_unswept_premium() == summary["unswept_premium"]
    assert get_unswept_tax_reserve() == summary["unswept_tax_reserve"]
