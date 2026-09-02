"""Tests for Module 7: Paper Ledger & Remote Kill Switch."""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from core.halt import halt, resume, is_halted, get_halt_status
from vesper.execution_guard import ExecutionGuard, TradingDisabled
from vesper.state import OrderProposal, ExecutionResult
from core.paper_ledger import (
    record_paper_fill,
    get_paper_positions,
    close_paper_position,
    mark_to_market,
    get_paper_summary,
    _load_ledger,
    _save_ledger,
)


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Isolate halt state and paper ledger in a temporary directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("core.halt._DATA_DIR", data_dir)
    monkeypatch.setattr("core.halt._HALT_STATE_PATH", data_dir / "halt_state.json")

    monkeypatch.setattr("core.paper_ledger._DATA_DIR", data_dir)
    monkeypatch.setattr("core.paper_ledger._LEDGER_PATH", data_dir / "paper_ledger.json")
    return data_dir


def test_halt_and_resume_lifecycle(temp_env):
    """Verify emergency halt freezes execution and resume restores readiness."""
    assert not is_halted()[0]

    halt_res = halt(reason="VIX spike circuit breaker", source="telegram_bot")
    assert halt_res["status"] == "HALTED"
    assert is_halted()[0]
    assert is_halted()[1]["reason"] == "VIX spike circuit breaker"

    status = get_halt_status()
    assert status["is_halted"]

    resume_res = resume(source="cli")
    assert resume_res["status"] == "ACTIVE"
    assert not is_halted()[0]


def test_execution_guard_blocks_when_halted(temp_env, monkeypatch):
    """Verify ExecutionGuard immediately blocks orders if halt is active even if trading is enabled."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    guard = ExecutionGuard()
    payload = {
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 5,
        "limit_price": 200.0,
        "asset_type": "EQUITY",
    }

    # Should preview cleanly before halt
    ticket = guard.preview("prop-1", payload)
    assert ticket is not None

    # Trigger emergency halt
    halt(reason="Flash crash protection", source="discord")

    # Should raise TradingDisabled
    with pytest.raises(TradingDisabled, match="Vesper is HALTED via emergency switch"):
        guard.preview("prop-2", payload)

    resume()
    # Now allowed again
    ticket2 = guard.preview("prop-2", payload)
    assert ticket2 is not None


@pytest.mark.asyncio
async def test_paper_ledger_fill_mark_to_market_and_close(temp_env):
    """Verify end-to-end paper trading ledger lifecycle."""
    prop = OrderProposal(
        id="prop-paper-1",
        ticker="NVDA",
        asset_type="EQUITY",
        side="BUY",
        limit_price=200.0,
        quantity=10,
        profit_target=220.0,
        stop_loss=190.0,
    )
    res = ExecutionResult(
        order_proposal_id="prop-paper-1",
        ticker="NVDA",
        status="DRY_RUN_SIMULATED",
        filled_quantity=10,
        filled_price=200.0,
    )

    fill = record_paper_fill(prop, res, session_id="sess-test-paper")
    assert fill["ticker"] == "NVDA"
    assert fill["total_cost"] == 2000.0

    positions = get_paper_positions()
    assert len(positions) == 1
    assert positions[0]["id"] == fill["id"]

    # Mark to market with +5% price gain ($210.00)
    mtm = await mark_to_market(live_quotes={"NVDA": 210.0})
    assert mtm["unrealized_pnl"] == 100.0  # (210 - 200) * 10
    assert mtm["total_nlv"] == 100_100.0

    summary = get_paper_summary()
    assert summary["unrealized_pnl"] == 100.0
    assert summary["open_positions_count"] == 1

    # Close position at $215.00
    closed = close_paper_position(fill["id"], close_price=215.0, reason="TAKE_PROFIT")
    assert closed is not None
    assert closed["status"] == "CLOSED"
    assert closed["realized_pnl"] == 150.0  # (215 - 200) * 10
    assert closed["realized_pnl_pct"] == 7.5

    summary_after = get_paper_summary()
    assert summary_after["open_positions_count"] == 0
    assert summary_after["closed_trades_count"] == 1
    assert summary_after["realized_pnl"] == 150.0
    assert summary_after["win_rate_pct"] == 100.0
