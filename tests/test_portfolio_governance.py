"""Tests for portfolio-level capital allocation buckets and their wiring into risk_gate_node.

Circuit-breaker halt behavior itself is covered in tests/test_circuit_breaker.py;
this file covers RiskEnforcer.check_capital_allocation_buckets (pure) and
risk_gate_node's end-to-end use of both the breaker and the buckets.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from vesper.risk import RiskEnforcer
from vesper.state import OrderProposal, TradingState
from vesper.nodes.risk_gate import risk_gate_node


# ── RiskEnforcer.check_capital_allocation_buckets (pure) ────────────────────

def _long_call(id_="prop-lc-1") -> OrderProposal:
    return OrderProposal(
        id=id_, ticker="NVDA", asset_type="OPTION", side="BUY",
        limit_price=8.50, quantity=1, strike=120.0, option_type="call",
        estimated_cost=850.0, max_risk=850.0,
    )


def _wheel_stock_buy(cost: float = 5000.0) -> OrderProposal:
    return OrderProposal(
        id="prop-wheel-stock", ticker="ULTY", asset_type="EQUITY", side="BUY",
        limit_price=10.0, quantity=int(cost / 10.0), strategy_type="WHEEL_ASSIGNMENT",
        estimated_cost=cost, max_risk=cost,
    )


def test_long_option_allowed_when_under_cap():
    ok, err = RiskEnforcer.check_capital_allocation_buckets(
        _long_call(), open_long_option_count=0, wheel_stock_notional=0.0, account_equity=50_000.0,
    )
    assert ok
    assert err is None


def test_long_option_blocked_at_cap():
    ok, err = RiskEnforcer.check_capital_allocation_buckets(
        _long_call(), open_long_option_count=1, wheel_stock_notional=0.0, account_equity=50_000.0,
    )
    assert not ok
    assert "1 open long option" in err


def test_short_option_never_blocked_by_long_option_cap():
    """A SELL (e.g. a CSP) isn't a long option position -- must not be blocked
    by the long-option bucket even with an existing long option open."""
    csp = OrderProposal(
        id="prop-csp", ticker="AAPL", asset_type="OPTION", side="SELL",
        limit_price=2.50, quantity=1, strike=190.0, option_type="put",
        estimated_cost=19000.0, max_risk=19000.0,
    )
    ok, err = RiskEnforcer.check_capital_allocation_buckets(
        csp, open_long_option_count=5, wheel_stock_notional=0.0, account_equity=50_000.0,
    )
    assert ok


def test_wheel_stock_buy_allowed_under_20pct_cap():
    ok, err = RiskEnforcer.check_capital_allocation_buckets(
        _wheel_stock_buy(5000.0), open_long_option_count=0, wheel_stock_notional=0.0,
        account_equity=50_000.0,  # 20% cap = $10,000; projected = $5,000
    )
    assert ok


def test_wheel_stock_buy_blocked_over_20pct_cap():
    ok, err = RiskEnforcer.check_capital_allocation_buckets(
        _wheel_stock_buy(6000.0), open_long_option_count=0, wheel_stock_notional=5000.0,
        account_equity=50_000.0,  # 20% cap = $10,000; projected = $11,000
    )
    assert not ok
    assert "wheel-stock" in err.lower()


def test_non_wheel_equity_buy_ignores_wheel_stock_cap():
    """An ordinary equity buy (no WHEEL_ASSIGNMENT tag) must not be blocked
    by the wheel-stock bucket even if wheel_stock_notional is already at cap."""
    plain_buy = OrderProposal(
        id="prop-plain", ticker="AAPL", asset_type="EQUITY", side="BUY",
        limit_price=200.0, quantity=100, estimated_cost=20_000.0, max_risk=20_000.0,
    )
    ok, err = RiskEnforcer.check_capital_allocation_buckets(
        plain_buy, open_long_option_count=0, wheel_stock_notional=15_000.0, account_equity=50_000.0,
    )
    assert ok


# ── risk_gate_node wiring ────────────────────────────────────────────────────

@pytest.fixture
def clean_paper_ledger(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("vesper.paper_ledger._DATA_DIR", data_dir)
    monkeypatch.setattr("vesper.paper_ledger._LEDGER_PATH", data_dir / "paper_ledger.json")
    monkeypatch.setattr("vesper.circuit_breaker._DATA_DIR", data_dir)
    monkeypatch.setattr("vesper.circuit_breaker._STATE_PATH", data_dir / "circuit_breaker_state.json")
    monkeypatch.setattr("vesper.halt._DATA_DIR", data_dir)
    monkeypatch.setattr("vesper.halt._HALT_STATE_PATH", data_dir / "halt_state.json")
    return data_dir


@pytest.mark.asyncio
async def test_risk_gate_second_long_option_in_same_batch_is_rejected(clean_paper_ledger):
    """Two long-option proposals drafted in the SAME pass must not both pass
    independently -- the first one approved counts against the second."""
    props = [_long_call("prop-lc-a"), _long_call("prop-lc-b")]
    state: TradingState = {
        "session_id": "sess-batch-longopt", "mode": "dry_run",
        "proposals": props, "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False):
        res = await risk_gate_node(state)

    assert len(res["proposals"]) == 1
    assert res["proposals"][0].id == "prop-lc-a"
    assert len(res["rejected_proposals"]) == 1
    assert "1 open long option" in res["rejected_proposals"][0].rejection_reason


@pytest.mark.asyncio
async def test_risk_gate_counts_existing_paper_long_option_against_new_one(clean_paper_ledger):
    """An already-open paper long-option position blocks a newly drafted one."""
    from vesper.paper_ledger import record_paper_fill
    from vesper.state import ExecutionResult

    existing = _long_call("prop-existing")
    record_paper_fill(
        proposal=existing,
        result=ExecutionResult(order_proposal_id=existing.id, ticker="NVDA", status="DRY_RUN_SIMULATED",
                                filled_quantity=1, filled_price=8.50),
    )

    new_prop = _long_call("prop-new")
    state: TradingState = {
        "session_id": "sess-existing-longopt", "mode": "dry_run",
        "proposals": [new_prop], "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False):
        res = await risk_gate_node(state)

    assert len(res["proposals"]) == 0
    assert len(res["rejected_proposals"]) == 1
    assert "1 open long option" in res["rejected_proposals"][0].rejection_reason


@pytest.mark.asyncio
async def test_risk_gate_wheel_stock_bucket_uses_paper_ledger_tagging(clean_paper_ledger):
    from vesper.paper_ledger import record_paper_fill
    from vesper.state import ExecutionResult

    with patch("vesper.account.fetch_live_equity", return_value=50_000.0):
        existing = _wheel_stock_buy(9000.0)  # already close to the $10k (20% of 50k) cap
        record_paper_fill(
            proposal=existing,
            result=ExecutionResult(order_proposal_id=existing.id, ticker="ULTY", status="DRY_RUN_SIMULATED",
                                    filled_quantity=existing.quantity, filled_price=10.0),
        )

        new_prop = _wheel_stock_buy(2000.0)  # would push total to $11,000 > $10,000 cap
        state: TradingState = {
            "session_id": "sess-wheel-cap", "mode": "dry_run",
            "proposals": [new_prop], "audit_trail": [],
        }
        with patch("vesper.llm.is_llm_enabled", return_value=False):
            res = await risk_gate_node(state)

    assert len(res["proposals"]) == 0
    assert "wheel-stock" in res["rejected_proposals"][0].rejection_reason.lower()


@pytest.mark.asyncio
async def test_risk_gate_tripped_circuit_breaker_note_and_halts(clean_paper_ledger):
    """A dry-run pass whose paper NLV has fallen >=15% from a previously
    recorded peak must trip the halt via risk_gate_node, with an audit note."""
    from vesper.paper_ledger import _load_ledger, _save_ledger
    from vesper.halt import is_halted

    ledger = _load_ledger()
    ledger["account"]["cash"] = 100_000.0
    ledger["account"]["total_nlv"] = 100_000.0
    _save_ledger(ledger)

    state: TradingState = {
        "session_id": "sess-breaker-1", "mode": "dry_run",
        "proposals": [], "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False):
        await risk_gate_node(state)  # establishes the peak at 100k
    assert not is_halted()[0]

    ledger = _load_ledger()
    ledger["account"]["total_nlv"] = 80_000.0  # -20%
    _save_ledger(ledger)

    state2: TradingState = {
        "session_id": "sess-breaker-2", "mode": "dry_run",
        "proposals": [], "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False):
        res2 = await risk_gate_node(state2)

    assert is_halted()[0]
    notes = res2["audit_trail"][0]["notes"]
    assert any("CIRCUIT BREAKER TRIPPED" in n for n in notes)


# ── capital_snapshot: per-proposal before/after diff (approval-card enrichment) ──

@pytest.mark.asyncio
async def test_risk_gate_capital_snapshot_reflects_stacked_state_not_final_totals(clean_paper_ledger):
    """Two long-option proposals in one batch: the SECOND proposal's snapshot
    must show open_long_option_count_before=1 (the first one's own stacking
    increment already applied), not the pre-batch 0 and not some
    bulk-carried-forward post-loop value that would be wrong for the first
    proposal too."""
    props = [_long_call("prop-lc-a"), _long_call("prop-lc-b")]
    state: TradingState = {
        "session_id": "sess-snapshot-batch", "mode": "dry_run",
        "proposals": props, "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False):
        res = await risk_gate_node(state)

    # prop-lc-a passes (first long option), prop-lc-b is rejected (would be
    # the second) -- but a snapshot is only captured for a proposal that
    # cleared the allocation-bucket check, so only prop-lc-a has one.
    snap = res["capital_snapshot"]
    assert snap["prop-lc-a"]["open_long_option_count_before"] == 0
    assert "prop-lc-b" not in snap


@pytest.mark.asyncio
async def test_risk_gate_capital_snapshot_second_proposal_sees_first_stacked(clean_paper_ledger):
    """Same batch shape, but with the second proposal drafted small enough
    to independently qualify were the cap not '1' -- verifies the *sector*
    half of the snapshot picks up the same-batch stacking too, by using
    two different tickers assumed to map to different (stubbed) sectors so
    both proposals clear check_capital_allocation_buckets and reach the
    sector-snapshot line."""
    prop_a = OrderProposal(
        id="prop-sec-a", ticker="AAPL", asset_type="EQUITY", side="BUY",
        limit_price=100.0, quantity=10, estimated_cost=1000.0, max_risk=1000.0,
    )
    prop_b = OrderProposal(
        id="prop-sec-b", ticker="MSFT", asset_type="EQUITY", side="BUY",
        limit_price=100.0, quantity=10, estimated_cost=1000.0, max_risk=1000.0,
    )
    state: TradingState = {
        "session_id": "sess-snapshot-sector", "mode": "dry_run",
        "proposals": [prop_a, prop_b], "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False):
        res = await risk_gate_node(state)

    snap = res["capital_snapshot"]
    assert len(res["proposals"]) == 2  # both cleared (different sectors, well under cap)
    assert snap["prop-sec-a"]["sector"] == "TEST_SECTOR_AAPL"
    assert snap["prop-sec-a"]["sector_notional_before"] == 0.0
    assert snap["prop-sec-b"]["sector"] == "TEST_SECTOR_MSFT"
    assert snap["prop-sec-b"]["sector_notional_before"] == 0.0  # different sector bucket, unaffected by prop_a


@pytest.mark.asyncio
async def test_risk_gate_sell_to_open_snapshot_has_no_sector(clean_paper_ledger):
    """A SELL-to-open (e.g. a CSP) never runs the sector lookup -- its
    capital_snapshot entry must carry sector=None, never a fabricated one."""
    csp = OrderProposal(
        id="prop-csp-snap", ticker="AAPL", asset_type="OPTION", side="SELL",
        limit_price=2.50, quantity=1, strike=90.0,
        estimated_cost=9000.0, max_risk=9000.0,
    )
    state: TradingState = {
        "session_id": "sess-snapshot-sell", "mode": "dry_run",
        "proposals": [csp], "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False):
        res = await risk_gate_node(state)

    assert len(res["proposals"]) == 1
    snap = res["capital_snapshot"]["prop-csp-snap"]
    assert snap["sector"] is None
    assert snap["sector_notional_before"] is None


@pytest.mark.asyncio
async def test_risk_gate_returns_account_equity_and_buying_power(clean_paper_ledger):
    with patch("vesper.nodes.risk_gate.fetch_live_equity", return_value=25_000.0), \
         patch("vesper.nodes.risk_gate.fetch_live_buying_power", return_value=12_500.0), \
         patch("vesper.llm.is_llm_enabled", return_value=False):
        state: TradingState = {
            "session_id": "sess-equity-bp", "mode": "dry_run",
            "proposals": [_long_call("prop-eq-1")], "audit_trail": [],
        }
        res = await risk_gate_node(state)

    assert res["account_equity"] == 25_000.0
    assert res["live_buying_power"] == 12_500.0


@pytest.mark.asyncio
async def test_risk_gate_missing_buying_power_is_none_not_fabricated(clean_paper_ledger):
    """fetch_live_buying_power failing/unconfigured must surface as None on
    the return dict, never a guessed number -- rule 1's fabrication ban
    applies here exactly as it does to account_equity's own fallback."""
    with patch("vesper.nodes.risk_gate.fetch_live_buying_power", return_value=None), \
         patch("vesper.llm.is_llm_enabled", return_value=False):
        state: TradingState = {
            "session_id": "sess-no-bp", "mode": "dry_run",
            "proposals": [_long_call("prop-eq-2")], "audit_trail": [],
        }
        res = await risk_gate_node(state)

    assert res["live_buying_power"] is None
