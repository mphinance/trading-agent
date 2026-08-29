"""Tests for the 15% sector-concentration capital allocation bucket.

Covers RiskEnforcer.check_sector_concentration (pure), its wiring into
risk_gate_node for both dry-run (paper ledger) and live (wb.portfolio())
modes, and vesper/sector.get_sector's caching behavior.

Circuit-breaker halt behavior and the pre-existing capital allocation
buckets (max open long options, wheel-stock 20% cap) are covered in
tests/test_circuit_breaker.py and tests/test_portfolio_governance.py; this
file covers only the sector bucket.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

import vesper.sector
from vesper.risk import RiskEnforcer
from vesper.state import OrderProposal, TradingState
from vesper.nodes.risk_gate import risk_gate_node


# ── RiskEnforcer.check_sector_concentration (pure) ──────────────────────────

def _buy(ticker: str = "AAPL", cost: float = 5000.0, id_: str = "prop-buy") -> OrderProposal:
    return OrderProposal(
        id=id_, ticker=ticker, asset_type="EQUITY", side="BUY",
        limit_price=100.0, quantity=int(cost / 100.0),
        estimated_cost=cost, max_risk=cost,
    )


def _sell(ticker: str = "AAPL", cost: float = 5000.0, id_: str = "prop-sell") -> OrderProposal:
    return OrderProposal(
        id=id_, ticker=ticker, asset_type="EQUITY", side="SELL",
        limit_price=100.0, quantity=int(cost / 100.0),
        estimated_cost=cost, max_risk=cost,
    )


def test_sector_buy_allowed_under_15pct_cap():
    ok, err = RiskEnforcer.check_sector_concentration(
        _buy(cost=5000.0), sector="Technology", sector_notional={}, account_equity=50_000.0,
        # 15% cap = $7,500; projected = $5,000
    )
    assert ok
    assert err is None


def test_sector_buy_blocked_over_15pct_cap():
    ok, err = RiskEnforcer.check_sector_concentration(
        _buy(cost=3000.0), sector="Technology", sector_notional={"Technology": 5000.0},
        account_equity=50_000.0,  # 15% cap = $7,500; projected = $8,000
    )
    assert not ok
    assert "Technology" in err
    assert "sector notional" in err


def test_sector_buy_at_exact_cap_boundary_passes():
    ok, err = RiskEnforcer.check_sector_concentration(
        _buy(cost=2500.0), sector="Technology", sector_notional={"Technology": 5000.0},
        account_equity=50_000.0,  # 15% cap = $7,500; projected = exactly $7,500
    )
    assert ok


def test_sector_none_on_buy_fails_closed():
    """A BUY that adds exposure but whose sector could not be resolved must
    REJECT, not silently pass -- rule 2 (fail closed on missing data). This
    is a deliberate divergence from the wheel-stock bucket's opt-in shape:
    that bucket only activates on an explicit strategy_type tag, so "no tag"
    correctly means "skip." Here, sector resolution failing is a DATA gap on
    a bucket that is otherwise always active for a BUY."""
    ok, err = RiskEnforcer.check_sector_concentration(
        _buy(), sector=None, sector_notional={}, account_equity=50_000.0,
    )
    assert not ok
    assert "could not resolve sector" in err
    assert "failing closed" in err


def test_sector_none_on_sell_passes():
    """Reducing/closing exposure never runs the sector guard, so an
    unresolvable sector on a SELL must not block it."""
    ok, err = RiskEnforcer.check_sector_concentration(
        _sell(), sector=None, sector_notional={}, account_equity=50_000.0,
    )
    assert ok
    assert err is None


def test_sector_closing_buy_bypasses_guard_even_with_sector_none():
    """A BUY tagged is_closing=True (closing a short) must bypass the guard
    entirely, same as a SELL. OrderProposal doesn't declare is_closing as a
    pydantic field -- risk.py reads it via getattr(proposal, "is_closing",
    False), the same duck-typed pattern execution_guard.py uses on its dict
    payloads -- so a MagicMock stands in here rather than a real
    OrderProposal, which (being a strict pydantic model) rejects assignment
    to an undeclared attribute."""
    prop = MagicMock()
    prop.side = "BUY"
    prop.is_closing = True
    ok, err = RiskEnforcer.check_sector_concentration(
        prop, sector=None, sector_notional={"Technology": 100_000.0}, account_equity=50_000.0,
    )
    assert ok
    assert err is None


# ── vesper/sector.get_sector caching ────────────────────────────────────────
#
# conftest.py's autouse _isolated_sector_cache fixture already resets
# vesper.sector._SECTOR_CACHE fresh for every test in the whole suite (a
# module-level in-process dict can leak cached values across tests exactly
# like an on-disk state file can -- see that fixture's docstring), so no
# local reset fixture is needed here.

def test_get_sector_returns_and_caches_value():
    mock_ticker = MagicMock()
    mock_ticker.info = {"sector": "Technology"}
    with patch("yfinance.Ticker", return_value=mock_ticker) as mock_ticker_cls:
        first = vesper.sector.get_sector("aapl")
        second = vesper.sector.get_sector("AAPL")

    assert first == "Technology"
    assert second == "Technology"
    # Only one real lookup -- the second call must be served from cache.
    mock_ticker_cls.assert_called_once_with("AAPL")


def test_get_sector_caches_none_result_too():
    mock_ticker = MagicMock()
    mock_ticker.info = {}
    with patch("yfinance.Ticker", return_value=mock_ticker) as mock_ticker_cls:
        first = vesper.sector.get_sector("ZZZZINVALID")
        second = vesper.sector.get_sector("ZZZZINVALID")

    assert first is None
    assert second is None
    mock_ticker_cls.assert_called_once()


def test_get_sector_never_raises_on_lookup_failure():
    with patch("yfinance.Ticker", side_effect=RuntimeError("network down")):
        result = vesper.sector.get_sector("AAPL")
    assert result is None


def test_get_sector_empty_ticker_returns_none_without_network_call():
    with patch("yfinance.Ticker") as mock_ticker_cls:
        assert vesper.sector.get_sector("") is None
        assert vesper.sector.get_sector("   ") is None
    mock_ticker_cls.assert_not_called()


# ── risk_gate_node wiring: dry-run (paper ledger) ───────────────────────────

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
async def test_risk_gate_dry_run_rejects_sector_breach(clean_paper_ledger):
    props = [_buy(ticker="AAPL", cost=8000.0, id_="prop-breach")]
    state: TradingState = {
        "session_id": "sess-sector-1", "mode": "dry_run",
        "proposals": props, "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False), \
         patch("vesper.nodes.risk_gate.fetch_live_equity", return_value=50_000.0), \
         patch("vesper.sector.get_sector", return_value="Technology"):
        res = await risk_gate_node(state)

    # 15% of $50,000 = $7,500 cap; an $8,000 buy alone breaches it.
    assert len(res["proposals"]) == 0
    assert len(res["rejected_proposals"]) == 1
    assert "Technology" in res["rejected_proposals"][0].rejection_reason
    assert "sector notional" in res["rejected_proposals"][0].rejection_reason


@pytest.mark.asyncio
async def test_risk_gate_second_same_sector_proposal_in_batch_stacks(clean_paper_ledger):
    """Two same-sector BUY proposals in the SAME pass must not both pass
    independently -- the first approved counts against the second, mirroring
    the existing open_long_option_count batch-stacking behavior."""
    props = [
        _buy(ticker="AAPL", cost=5000.0, id_="prop-a"),
        _buy(ticker="MSFT", cost=5000.0, id_="prop-b"),
    ]
    state: TradingState = {
        "session_id": "sess-sector-batch", "mode": "dry_run",
        "proposals": props, "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False), \
         patch("vesper.nodes.risk_gate.fetch_live_equity", return_value=50_000.0), \
         patch("vesper.sector.get_sector", return_value="Technology"):
        res = await risk_gate_node(state)

    # 15% of $50,000 = $7,500 cap. First $5,000 buy passes; the second
    # would bring the (same-sector) total to $10,000 > $7,500.
    assert len(res["proposals"]) == 1
    assert res["proposals"][0].id == "prop-a"
    assert len(res["rejected_proposals"]) == 1
    assert res["rejected_proposals"][0].id == "prop-b"
    assert "Technology" in res["rejected_proposals"][0].rejection_reason


@pytest.mark.asyncio
async def test_risk_gate_dry_run_counts_existing_paper_position_by_sector(clean_paper_ledger):
    """An already-open paper position's notional counts toward its sector's
    cap for a newly drafted proposal in the same sector."""
    from vesper.paper_ledger import record_paper_fill
    from vesper.state import ExecutionResult

    existing = _buy(ticker="NVDA", cost=6000.0, id_="prop-existing")
    with patch("vesper.sector.get_sector", return_value="Technology"):
        record_paper_fill(
            proposal=existing,
            result=ExecutionResult(order_proposal_id=existing.id, ticker="NVDA", status="DRY_RUN_SIMULATED",
                                    filled_quantity=existing.quantity, filled_price=100.0),
        )

    new_prop = _buy(ticker="AAPL", cost=2000.0, id_="prop-new")
    state: TradingState = {
        "session_id": "sess-sector-existing", "mode": "dry_run",
        "proposals": [new_prop], "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False), \
         patch("vesper.nodes.risk_gate.fetch_live_equity", return_value=50_000.0), \
         patch("vesper.sector.get_sector", return_value="Technology"):
        res = await risk_gate_node(state)

    # 15% of $50,000 = $7,500 cap; $6,000 existing + $2,000 new = $8,000 > cap.
    assert len(res["proposals"]) == 0
    assert "Technology" in res["rejected_proposals"][0].rejection_reason


@pytest.mark.asyncio
async def test_risk_gate_dry_run_sell_never_blocked_by_sector_cap(clean_paper_ledger):
    """A SELL proposal must never be rejected by the sector-concentration
    bucket, even when the sector is already at/over cap and even when the
    sector can't be resolved."""
    sell_prop = _sell(ticker="AAPL", cost=1000.0)
    state: TradingState = {
        "session_id": "sess-sector-sell", "mode": "dry_run",
        "proposals": [sell_prop], "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False), \
         patch("vesper.nodes.risk_gate.fetch_live_equity", return_value=50_000.0), \
         patch("vesper.sector.get_sector", return_value=None) as mock_get_sector:
        res = await risk_gate_node(state)

    assert len(res["proposals"]) == 1
    # The sector lookup should never even be invoked for a proposal that
    # doesn't add exposure -- no point paying for a network call whose
    # result is thrown away.
    mock_get_sector.assert_not_called()


@pytest.mark.asyncio
async def test_risk_gate_dry_run_rejects_when_sector_unresolvable(clean_paper_ledger):
    props = [_buy(ticker="ZZZZINVALID", cost=1000.0)]
    state: TradingState = {
        "session_id": "sess-sector-unresolved", "mode": "dry_run",
        "proposals": props, "audit_trail": [],
    }
    with patch("vesper.llm.is_llm_enabled", return_value=False), \
         patch("vesper.nodes.risk_gate.fetch_live_equity", return_value=50_000.0), \
         patch("vesper.sector.get_sector", return_value=None):
        res = await risk_gate_node(state)

    assert len(res["proposals"]) == 0
    assert "could not resolve sector" in res["rejected_proposals"][0].rejection_reason


# ── risk_gate_node wiring: live mode (wb.portfolio()) ───────────────────────

@pytest.mark.asyncio
async def test_risk_gate_live_mode_counts_equity_and_skips_option_with_audit_note():
    mock_wb = MagicMock()
    mock_wb.configured = True
    mock_wb.portfolio.return_value = {
        "totals": {"nlv": 50_000.0},
        "accounts": [
            {
                "positions": [
                    {"symbol": "AAPL", "instrument_type": "EQUITY", "market_value": 5000.0, "quantity": 10},
                    {"symbol": "NVDA240920C00120000", "instrument_type": "OPTION",
                     "market_value": 2000.0, "quantity": 1},
                ]
            }
        ],
    }

    new_prop = _buy(ticker="AAPL", cost=3000.0, id_="prop-live-new")
    state: TradingState = {
        "session_id": "sess-sector-live", "mode": "live",
        "proposals": [new_prop], "audit_trail": [],
    }
    with patch("wb.Webull", return_value=mock_wb), \
         patch("vesper.llm.is_llm_enabled", return_value=False), \
         patch("vesper.sector.get_sector", return_value="Technology"):
        res = await risk_gate_node(state)

    # 15% of $50,000 = $7,500 cap; $5,000 existing AAPL (EQUITY, counted) +
    # $3,000 new = $8,000 > cap -- rejected.
    assert len(res["proposals"]) == 0
    assert "Technology" in res["rejected_proposals"][0].rejection_reason

    notes = res["audit_trail"][0]["notes"]
    assert any("does not count live OPTION positions" in n for n in notes)


@pytest.mark.asyncio
async def test_risk_gate_live_mode_equity_only_under_cap_passes():
    mock_wb = MagicMock()
    mock_wb.configured = True
    mock_wb.portfolio.return_value = {
        "totals": {"nlv": 50_000.0},
        "accounts": [{"positions": [
            {"symbol": "AAPL", "instrument_type": "EQUITY", "market_value": 1000.0, "quantity": 5},
        ]}],
    }

    new_prop = _buy(ticker="AAPL", cost=1000.0, id_="prop-live-ok")
    state: TradingState = {
        "session_id": "sess-sector-live-ok", "mode": "live",
        "proposals": [new_prop], "audit_trail": [],
    }
    with patch("wb.Webull", return_value=mock_wb), \
         patch("vesper.llm.is_llm_enabled", return_value=False), \
         patch("vesper.sector.get_sector", return_value="Technology"):
        res = await risk_gate_node(state)

    assert len(res["proposals"]) == 1
    assert res["proposals"][0].id == "prop-live-ok"
