"""Integration tests for the execution pipeline: risk_gate_node -> executor_node.

Verifies end-to-end wiring across the compiled graph execution path:
1. Valid approved proposal in live mode reaches broker place_order when VESPER_TRADING=1.
2. Kill switch (VESPER_TRADING=0) blocks the order before any broker call is made.
3. Notional cap violation blocks the order before any broker call is made.
4. Dry-run mode bypasses broker completely and records fills in paper ledger.
5. Multi-leg synthetic long combo in dry-run mode books both legs in paper ledger.
6. Risk gate filters out invalid proposals while forwarding valid proposals to execution.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from vesper.nodes.executor import executor_node
from vesper.nodes.risk_gate import risk_gate_node
from vesper.paper_ledger import _load_ledger, get_paper_summary
from vesper.state import OrderLeg, OrderProposal, TradingState


@pytest.fixture
def clean_paper_ledger(tmp_path, monkeypatch):
    """Isolate paper ledger storage for integration tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = data_dir / "paper_ledger.json"
    monkeypatch.setattr("vesper.paper_ledger._DATA_DIR", data_dir)
    monkeypatch.setattr("vesper.paper_ledger._LEDGER_PATH", ledger_path)
    return ledger_path


@pytest.fixture
def mock_wb():
    """Mock Webull client with valid credentials, account list, and buying power."""
    with patch("wb.Webull") as mock_wb_cls:
        wb = MagicMock()
        wb.configured = True
        wb.trade.account_v2.get_account_list.return_value = {
            "data": [{"account_id": "ACC-TEST-12345", "account_class": "INDIVIDUAL_CASH"}]
        }
        wb.portfolio.return_value = {
            "totals": {"buying_power": 50_000.0}
        }
        wb.trade.order_v2.place_order.return_value = {"data": {"order_id": "wb-ord-999"}}
        mock_wb_cls.return_value = wb
        yield wb


def _make_valid_equity_proposal(ticker: str = "AAPL", price: float = 150.0, qty: int = 10) -> OrderProposal:
    """A valid equity proposal with compliant stop-loss, profit-target, and risk-reward."""
    cost = round(price * qty, 2)
    return OrderProposal(
        id=f"prop-test-{ticker.lower()}",
        ticker=ticker,
        asset_type="EQUITY",
        side="BUY",
        order_type="LIMIT",
        quantity=qty,
        limit_price=price,
        stop_loss=round(price * 0.95, 2),       # 5% stop
        profit_target=round(price * 1.10, 2),   # 10% target -> 2.0 R:R
        estimated_cost=cost,
        max_risk=round(cost * 0.05, 2),
        risk_reward_ratio=2.0,
    )


def _make_synthetic_long_proposal() -> OrderProposal:
    """A valid multi-leg synthetic long combo proposal."""
    return OrderProposal(
        id="prop-test-synth-nvda",
        ticker="NVDA",
        asset_type="OPTION",
        side="BUY",
        limit_price=8.50,
        strike=120.0,
        expiry="2025-09-19",
        option_type="call",
        strategy_type="SYNTHETIC_LONG",
        legs=[
            OrderLeg(
                side="BUY",
                option_type="call",
                strike=120.0,
                expiry="2025-09-19",
                quantity=1,
                limit_price=8.50,
                contract_symbol="NVDA250919C00120000",
            ),
            OrderLeg(
                side="SELL",
                option_type="put",
                strike=120.0,
                expiry="2025-09-19",
                quantity=1,
                limit_price=6.20,
                contract_symbol="NVDA250919P00120000",
            ),
        ],
        stop_loss=4.25,
        profit_target=17.00,
        estimated_cost=12000.0,
        max_risk=12000.0,
        risk_reward_ratio=2.0,
    )


@pytest.mark.asyncio
async def test_end_to_end_single_leg_live_execution_submitted(mock_wb, monkeypatch):
    """Valid proposal passes risk_gate_node -> approved -> executor_node in mode=live with VESPER_TRADING=1.
    Broker place_order must be invoked and result must be SUBMITTED."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("EXECUTION_BROKER", "webull")
    monkeypatch.delenv("VESPER_KILL_SWITCH", raising=False)

    prop = _make_valid_equity_proposal("AAPL", 150.0, 10)

    # 1. Run through risk gate node
    state: TradingState = {
        "session_id": "sess-live-exec",
        "mode": "live",
        "proposals": [prop],
        "audit_trail": [],
    }

    with patch("vesper.nodes.risk_gate.fetch_live_equity", return_value=50000.0):
        with patch("vesper.llm.is_llm_enabled", return_value=False):
            rg_out = await risk_gate_node(state)

    assert len(rg_out["proposals"]) == 1
    passed_prop = rg_out["proposals"][0]

    # 2. Simulate human approval
    passed_prop.approved = True
    state["proposals"] = [passed_prop]

    # 3. Run through executor node
    exec_out = await executor_node(state)

    results = exec_out["execution_results"]
    assert len(results) == 1
    assert results[0].status == "SUBMITTED"
    assert results[0].ticker == "AAPL"

    # Broker place_order was genuinely called
    mock_wb.trade.order_v2.place_order.assert_called_once()
    called_payload = mock_wb.trade.order_v2.place_order.call_args[0][0]
    assert called_payload["symbol"] == "AAPL"
    assert called_payload["side"] == "BUY"
    assert called_payload["quantity"] == 10
    assert called_payload["limit_price"] == 150.0


@pytest.mark.asyncio
async def test_end_to_end_kill_switch_blocks_broker_call(mock_wb, monkeypatch):
    """When VESPER_TRADING is unset (or 0), execution_guard blocks the order before any broker call."""
    monkeypatch.setenv("VESPER_TRADING", "0")
    monkeypatch.setenv("EXECUTION_BROKER", "webull")

    prop = _make_valid_equity_proposal("AAPL", 150.0, 10)
    prop.approved = True

    state: TradingState = {
        "session_id": "sess-blocked-killswitch",
        "mode": "live",
        "proposals": [prop],
        "audit_trail": [],
    }

    exec_out = await executor_node(state)
    results = exec_out["execution_results"]

    assert len(results) == 1
    assert results[0].status == "BLOCKED_BY_GUARDRAIL"
    assert "disabled" in results[0].message.lower()

    # Broker place_order must NEVER be called
    mock_wb.trade.order_v2.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_end_to_end_max_notional_blocks_broker_call(mock_wb, monkeypatch):
    """When a proposal exceeds VESPER_MAX_NOTIONAL, execution_guard blocks it before broker call."""
    monkeypatch.setenv("VESPER_TRADING", "1")
    monkeypatch.setenv("EXECUTION_BROKER", "webull")
    monkeypatch.setenv("VESPER_MAX_NOTIONAL", "1000")  # Cap at $1,000

    # Proposal notional = 150 * 10 = $1,500 > $1,000 cap
    prop = _make_valid_equity_proposal("AAPL", 150.0, 10)
    prop.approved = True

    state: TradingState = {
        "session_id": "sess-blocked-notional",
        "mode": "live",
        "proposals": [prop],
        "audit_trail": [],
    }

    exec_out = await executor_node(state)
    results = exec_out["execution_results"]

    assert len(results) == 1
    assert results[0].status == "BLOCKED_BY_GUARDRAIL"
    assert "exceeds vesper_max_notional" in results[0].message.lower()

    # Broker place_order must NEVER be called
    mock_wb.trade.order_v2.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_end_to_end_dry_run_records_fill_without_broker_call(
    clean_paper_ledger, mock_wb, monkeypatch
):
    """In mode=dry_run, executor_node records paper fill and never touches broker."""
    monkeypatch.setenv("VESPER_TRADING", "1")

    prop = _make_valid_equity_proposal("MSFT", 400.0, 5)
    prop.approved = True

    state: TradingState = {
        "session_id": "sess-dry-run-exec",
        "mode": "dry_run",
        "proposals": [prop],
        "audit_trail": [],
    }

    exec_out = await executor_node(state)
    results = exec_out["execution_results"]

    assert len(results) == 1
    assert results[0].status == "DRY_RUN_SIMULATED"
    assert results[0].filled_quantity == 5
    assert results[0].filled_price == 400.0

    # Broker place_order must NOT be called
    mock_wb.trade.order_v2.place_order.assert_not_called()

    # Fill must land in paper ledger
    summary = get_paper_summary()
    assert summary["open_positions_count"] == 1


@pytest.mark.asyncio
async def test_end_to_end_multileg_synthetic_long_dry_run(
    clean_paper_ledger, mock_wb, monkeypatch
):
    """Multi-leg synthetic long combo in dry-run mode routes through executor_node
    and books both legs in paper ledger."""
    prop = _make_synthetic_long_proposal()
    prop.approved = True

    state: TradingState = {
        "session_id": "sess-synth-dryrun",
        "mode": "dry_run",
        "proposals": [prop],
        "audit_trail": [],
    }

    exec_out = await executor_node(state)
    results = exec_out["execution_results"]

    assert len(results) == 1
    assert results[0].status == "DRY_RUN_SIMULATED"

    # Broker place_order must NOT be called
    mock_wb.trade.order_v2.place_order.assert_not_called()

    # Both legs must be recorded in paper ledger
    ledger = _load_ledger()
    fills = [f for f in ledger["fills"] if f["order_proposal_id"] == prop.id]
    assert len(fills) == 2
    types = {f["option_type"] for f in fills}
    assert types == {"call", "put"}


@pytest.mark.asyncio
async def test_risk_gate_filters_invalid_and_forwards_valid(clean_paper_ledger, monkeypatch):
    """risk_gate_node rejects non-compliant proposal and allows compliant proposal to proceed."""
    valid_prop = _make_valid_equity_proposal("SPY", 550.0, 2)

    # Invalid proposal: limit_price <= 0 violates RiskEnforcer deterministic check
    invalid_prop = OrderProposal(
        id="prop-test-bad-price",
        ticker="QQQ",
        asset_type="EQUITY",
        side="BUY",
        order_type="LIMIT",
        quantity=2,
        limit_price=0.0,  # Invalid: <= 0
        stop_loss=0.0,
        profit_target=520.0,
        estimated_cost=0.0,
        max_risk=0.0,
        risk_reward_ratio=1.5,
    )

    state: TradingState = {
        "session_id": "sess-filter-test",
        "mode": "dry_run",
        "proposals": [valid_prop, invalid_prop],
        "audit_trail": [],
    }

    with patch("vesper.nodes.risk_gate.fetch_live_equity", return_value=50000.0):
        with patch("vesper.llm.is_llm_enabled", return_value=False):
            rg_out = await risk_gate_node(state)

    assert len(rg_out["proposals"]) == 1
    assert rg_out["proposals"][0].ticker == "SPY"
    assert len(rg_out["rejected_proposals"]) == 1
    assert rg_out["rejected_proposals"][0].ticker == "QQQ"
    assert "limit price" in rg_out["rejected_proposals"][0].rejection_reason.lower()

    # Approve the valid proposal and execute
    rg_out["proposals"][0].approved = True
    state["proposals"] = rg_out["proposals"]

    exec_out = await executor_node(state)
    assert len(exec_out["execution_results"]) == 1
    assert exec_out["execution_results"][0].ticker == "SPY"
    assert exec_out["execution_results"][0].status == "DRY_RUN_SIMULATED"


@pytest.mark.asyncio
async def test_dry_run_records_no_paper_fill_while_halted(clean_paper_ledger, mock_wb):
    """A halt must stop the dry-run path too. It bypasses execution_guard (no
    broker call to guard), so it needs its own is_halted() check -- without it a
    resume landing during a freeze still wrote a paper fill, which then fed
    circuit_breaker's own NLV/drawdown maths."""
    from vesper.halt import halt, resume as clear_halt
    from vesper.paper_ledger import get_paper_summary

    prop = _make_valid_equity_proposal("MSFT", 400.0, 5)
    prop.approved = True
    state: TradingState = {
        "session_id": "sess-halted-dryrun", "mode": "dry_run",
        "proposals": [prop], "audit_trail": [],
    }

    halt(reason="test freeze", source="test")
    try:
        exec_out = await executor_node(state)
    finally:
        clear_halt(source="test")

    results = exec_out["execution_results"]
    assert len(results) == 1
    assert results[0].status == "BLOCKED_BY_GUARDRAIL"
    assert "halted" in results[0].message.lower()
    assert get_paper_summary()["open_positions_count"] == 0, "no fill may be recorded while halted"
