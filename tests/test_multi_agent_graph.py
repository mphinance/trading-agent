"""Integration tests for Vesper Multi-Agent Swarm and Debate Synthesis LangGraph execution."""

from __future__ import annotations

import pytest
import edgar
from vesper.state import (
    Candidate,
    MarketRegime,
    OptionAudit,
    OrderProposal,
    TechnicalAudit,
    TradingState,
)
from vesper.nodes import swarm_node, synthesis_node
from vesper.graph import build_trading_graph


@pytest.fixture(autouse=True)
def _no_edgar_network(monkeypatch):
    """See tests/test_agents_specialists.py's fixture of the same name:
    swarm_node runs FundamentalAgent, whose best-effort EDGAR lookup would
    otherwise be a real network call to sec.gov whenever SEC_USER_AGENT has
    leaked into the environment from another test module's dotenv load.
    """

    def _no_network(*args, **kwargs):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(edgar, "_get", _no_network)


@pytest.fixture
def mock_graph_state() -> TradingState:
    regime = MarketRegime(
        posture="BULLISH",
        health_score=6.0,
        health_label="STRONG_BULL",
        spy_spot=550.0,
        spy_gex_regime="POSITIVE",
        spy_gamma_flip=540.0,
    )
    candidate = Candidate(
        ticker="MSFT",
        source="MOMENTUM_SQUEEZE",
        score=90.0,
        rationale="Strong trend breakout",
        catalyst="Cloud growth",
        data={},
    )
    tech = TechnicalAudit(
        ticker="MSFT",
        close=420.0,
        rsi_14=65.0,
        rsi_state="bullish",
        macd_signal="BULLISH",
        ema_stack="BULLISH",
        ema_8=418.0,
        ema_21=415.0,
        ema_34=410.0,
        ema_55=405.0,
        ema_89=400.0,
        atr_14=6.0,
        adx_14=30.0,
    )
    opt = OptionAudit(
        ticker="MSFT",
        option_type="call",
        strike=425.0,
        expiry="2026-09-18",
        dte=14,
        delta=0.45,
        vopr_grade="A",
    )
    proposal = OrderProposal(
        id="prop-msft-1",
        ticker="MSFT",
        asset_type="OPTION",
        side="BUY",
        limit_price=4.50,
        stop_loss=2.50,
        profit_target=8.00,
        risk_reward_ratio=2.2,
    )
    return {
        "session_id": "test-multi-agent-session",
        "mode": "dry_run",
        "selected_playbook": "momentum_squeeze",
        "target_ticker": "MSFT",
        "regime": regime,
        "candidates": [candidate],
        "technicals": {"MSFT": tech},
        "options_audits": {"MSFT": opt},
        "worker_reports": {},
        "active_workers": [],
        "debate_transcripts": [],
        "agent_conviction_weights": None,
        "proposals": [proposal],
        "rejected_proposals": [],
        "execution_results": [],
        "account_equity": 25000.0,
        "live_buying_power": 100000.0,
        "capital_snapshot": None,
        "needs_human_approval": False,
        "human_decision": None,
        "persona": "default",
        "audit_trail": [],
        "reflection_notes": [],
        "errors": [],
    }


@pytest.mark.asyncio
async def test_swarm_node_execution(mock_graph_state):
    output = await swarm_node(mock_graph_state)
    assert "worker_reports" in output
    assert "MSFT" in output["worker_reports"]
    reports = output["worker_reports"]["MSFT"]
    assert len(reports) >= 3  # Multiple specialist reports generated in parallel
    assert "active_workers" in output
    assert len(output["audit_trail"]) > 0


@pytest.mark.asyncio
async def test_synthesis_node_execution(mock_graph_state):
    # First get worker reports
    swarm_out = await swarm_node(mock_graph_state)
    mock_graph_state["worker_reports"] = swarm_out["worker_reports"]

    synth_out = await synthesis_node(mock_graph_state)
    assert "debate_transcripts" in synth_out
    assert len(synth_out["debate_transcripts"]) == 1
    assert "proposals" in synth_out
    assert len(synth_out["proposals"]) == 1
    proposal = synth_out["proposals"][0]
    assert proposal.thesis_source == "vesper/swarm_debate"
    assert "Swarm Consensus" in (proposal.thesis or "")


@pytest.mark.asyncio
async def test_build_full_multi_agent_trading_graph():
    app = await build_trading_graph(checkpointer=False)
    # Validate node existence
    nodes = app.nodes
    assert "regime_node" in nodes
    assert "scanner_node" in nodes
    assert "analyst_node" in nodes
    assert "swarm_node" in nodes
    assert "playbooks_node" in nodes
    assert "synthesis_node" in nodes
    assert "risk_gate_node" in nodes
    assert "human_gate_node" in nodes
    assert "executor_node" in nodes
    assert "reflection_node" in nodes
