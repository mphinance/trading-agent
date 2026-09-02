"""Tests for Vesper specialist agents, supervisor, debate synthesis, and risk adversary."""

from __future__ import annotations

import pytest
import core.edgar as edgar
from vesper.state import (
    Candidate,
    MarketRegime,
    OptionAudit,
    OrderProposal,
    TechnicalAudit,
    TradingState,
    WorkerReport,
)
from vesper.agents import (
    TechnicalAnalystAgent,
    InstitutionalFlowAgent,
    FundamentalAgent,
    GammaStructureAgent,
    MacroSupervisor,
    DebateSynthesisSupervisor,
    AdversarialRiskAgent,
)


@pytest.fixture(autouse=True)
def _no_edgar_network(monkeypatch):
    """FundamentalAgent.analyze() does a best-effort SEC EDGAR filings lookup
    through edgar.filings(), which goes through edgar._get for every HTTP
    call -- the same seam tests/test_edgar.py monkeypatches. Left unpatched,
    a real .env with SEC_USER_AGENT set (loaded as a side effect of
    trading_mcp/server.py being imported elsewhere in the same pytest
    session) makes this a real network call to sec.gov, which is exactly the
    class of hermetic-suite leak fixed in b0019ff. The agent already
    try/excepts this call and degrades to skipping the EDGAR catalysts, so
    raising here just exercises that path deterministically instead of
    depending on live network access.
    """

    def _no_network(*args, **kwargs):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(edgar, "_get", _no_network)


@pytest.fixture
def mock_state() -> TradingState:
    regime = MarketRegime(
        posture="BULLISH",
        health_score=5.5,
        health_label="STRONG_BULL",
        spy_spot=550.0,
        spy_gex_regime="POSITIVE",
        spy_gamma_flip=540.0,
    )
    candidate = Candidate(
        ticker="AAPL",
        source="0DTE_FLOW",
        score=85.0,
        rationale="Strong institutional buying",
        catalyst="AI Product Launch",
        data={
            "unusual_activity": [
                {
                    "size": 5000,
                    "open_interest": 1000,
                    "iv": 0.35,
                    "type": "CALL",
                    "sentiment": "Bullish",
                }
            ],
            "apex_levels": {"s1": 220.0, "r1": 235.0},
        },
    )
    tech = TechnicalAudit(
        ticker="AAPL",
        close=225.0,
        rsi_14=62.0,
        rsi_state="bullish",
        macd_signal="BULLISH",
        ema_stack="BULLISH",
        ema_8=224.0,
        ema_21=222.0,
        ema_34=220.0,
        ema_55=218.0,
        ema_89=215.0,
        atr_14=3.5,
        adx_14=28.0,
        rsi_2=8.5,
        sma_200=210.0,
    )
    opt = OptionAudit(
        ticker="AAPL",
        option_type="call",
        strike=230.0,
        expiry="2026-09-18",
        dte=14,
        delta=0.45,
        theta=-0.12,
        iv=0.28,
        vopr_grade="A",
    )
    return {
        "session_id": "test-session-123",
        "mode": "dry_run",
        "selected_playbook": "all",
        "target_ticker": "AAPL",
        "regime": regime,
        "candidates": [candidate],
        "technicals": {"AAPL": tech},
        "options_audits": {"AAPL": opt},
        "worker_reports": {},
        "active_workers": [],
        "debate_transcripts": [],
        "agent_conviction_weights": None,
        "proposals": [],
        "rejected_proposals": [],
        "execution_results": [],
        "account_equity": 10000.0,
        "live_buying_power": 50000.0,
        "capital_snapshot": None,
        "needs_human_approval": False,
        "human_decision": None,
        "persona": "default",
        "audit_trail": [],
        "reflection_notes": [],
        "errors": [],
    }


@pytest.mark.asyncio
async def test_technical_analyst_agent(mock_state):
    agent = TechnicalAnalystAgent()
    report = await agent.analyze("AAPL", mock_state)
    assert report.agent_name == "technical_agent"
    assert report.ticker == "AAPL"
    assert report.direction == "BULLISH"
    assert report.confidence_score >= 65.0
    assert len(report.key_catalysts) >= 2
    assert 220.0 in report.invalidation_levels or 210.0 in report.invalidation_levels


@pytest.mark.asyncio
async def test_institutional_flow_agent(mock_state):
    agent = InstitutionalFlowAgent()
    report = await agent.analyze("AAPL", mock_state)
    assert report.agent_name == "flow_agent"
    assert report.ticker == "AAPL"
    assert report.direction == "BULLISH"
    assert report.time_horizon == "INTRADAY_0DTE"
    assert report.confidence_score >= 65.0


@pytest.mark.asyncio
async def test_fundamental_agent(mock_state):
    agent = FundamentalAgent()
    report = await agent.analyze("AAPL", mock_state)
    assert report.agent_name == "fundamental_agent"
    assert report.ticker == "AAPL"
    assert report.direction in ("BULLISH", "NEUTRAL")
    assert report.confidence_score >= 50.0


@pytest.mark.asyncio
async def test_gamma_structure_agent(mock_state):
    agent = GammaStructureAgent()
    report = await agent.analyze("AAPL", mock_state)
    assert report.agent_name == "gamma_agent"
    assert report.ticker == "AAPL"
    assert report.direction == "BULLISH"
    assert 230.0 in report.invalidation_levels or 220.0 in report.invalidation_levels


def test_macro_supervisor(mock_state):
    supervisor = MacroSupervisor()
    workers = supervisor.select_active_workers(mock_state)
    assert "technical_agent" in workers
    assert "flow_agent" in workers
    assert "fundamental_agent" in workers
    assert "gamma_agent" in workers

    # Test defensive mode activation
    defensive_state = dict(mock_state)
    defensive_state["regime"] = MarketRegime(posture="HIGH_RISK_DISTRIBUTION")
    eligible_playbooks = supervisor.filter_eligible_playbooks(defensive_state)
    assert "momentum_squeeze" not in eligible_playbooks
    assert "collar_following" in eligible_playbooks


def test_debate_synthesis_consensus():
    supervisor = DebateSynthesisSupervisor()
    reports = [
        WorkerReport(
            agent_name="technical_agent",
            ticker="NVDA",
            direction="BULLISH",
            confidence_score=85.0,
            thesis_summary="Breakout above resistance",
        ),
        WorkerReport(
            agent_name="flow_agent",
            ticker="NVDA",
            direction="BULLISH",
            confidence_score=80.0,
            thesis_summary="Institutional call buying",
        ),
    ]
    direction, conf, thesis, transcript = supervisor.resolve_ticker_debate("NVDA", reports)
    assert direction == "BULLISH"
    assert conf >= 80.0
    assert not transcript["has_conflict"]
    assert "NVDA" in transcript["ticker"]


def test_debate_synthesis_conflict_arbitration():
    supervisor = DebateSynthesisSupervisor()
    reports = [
        WorkerReport(
            agent_name="technical_agent",
            ticker="TSLA",
            direction="BULLISH",
            confidence_score=80.0,
            thesis_summary="Oversold bounce",
        ),
        WorkerReport(
            agent_name="fundamental_agent",
            ticker="TSLA",
            direction="BEARISH",
            confidence_score=75.0,
            thesis_summary="Margin compression",
        ),
    ]
    direction, conf, thesis, transcript = supervisor.resolve_ticker_debate("TSLA", reports)
    assert transcript["has_conflict"] is True
    assert len(transcript["bullish_arguments"]) == 1
    assert len(transcript["bearish_arguments"]) == 1
    assert "Arbitrated" in thesis


def test_adversarial_risk_agent(mock_state):
    adversary = AdversarialRiskAgent()
    good_proposal = OrderProposal(
        id="prop-1",
        ticker="AAPL",
        asset_type="OPTION",
        side="BUY",
        limit_price=2.50,
        stop_loss=1.80,
        profit_target=4.50,
        risk_reward_ratio=2.85,
    )
    result = adversary.red_team_proposal(good_proposal, mock_state)
    assert result["verdict"] == "CLEARED"
    assert len(result["risk_flags"]) == 0

    bad_proposal = OrderProposal(
        id="prop-2",
        ticker="AAPL",
        asset_type="EQUITY",
        side="BUY",
        limit_price=225.0,
        stop_loss=None,  # missing stop
        risk_reward_ratio=1.1,  # poor risk reward
    )
    bad_result = adversary.red_team_proposal(bad_proposal, mock_state)
    assert bad_result["verdict"] == "WARNING"
    assert len(bad_result["risk_flags"]) >= 2
