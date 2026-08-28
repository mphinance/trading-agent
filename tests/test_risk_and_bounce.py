"""Tests for Volatility-Targeting Position Sizing & Bounce 2.0 Playbook."""

from __future__ import annotations

import pytest
from vesper.risk import RiskEnforcer
from vesper.state import TechnicalAudit, MarketRegime, TradingState
from vesper.nodes.playbooks import playbooks_node


def test_vol_targeted_sizing_scales_inversely_with_realized_volatility():
    """Verify that high volatility stock receives smaller position than low volatility stock."""
    equity = 50_000.0
    entry_px = 100.0

    # Low vol stock (ATR = $1.00 -> 1% daily vol, stop = 1.5 ATR = $98.50)
    shares_low_vol, cost_low_vol, risk_low_vol = RiskEnforcer.calculate_vol_targeted_size(
        account_equity=equity,
        entry_price=entry_px,
        stop_loss_price=98.50,
        target_price=103.00,
        atr_14=1.0,
    )

    # High vol stock (ATR = $5.00 -> 5% daily vol, stop = 1.5 ATR = $92.50)
    shares_high_vol, cost_high_vol, risk_high_vol = RiskEnforcer.calculate_vol_targeted_size(
        account_equity=equity,
        entry_price=entry_px,
        stop_loss_price=92.50,
        target_price=115.00,
        atr_14=5.0,
    )

    # High vol stock must receive fewer shares and less total capital allocation
    assert shares_low_vol > shares_high_vol
    assert cost_low_vol > cost_high_vol


@pytest.mark.asyncio
async def test_playbooks_node_synthesizes_bounce_2_proposal():
    """Verify playbooks_node drafts Bounce 2.0 pullback proposal with action zone stops and targets."""
    tech_nvda = TechnicalAudit(
        ticker="NVDA",
        close=200.0,
        rsi_14=52.0,
        rsi_state="neutral",
        ema_stack="BULLISH",
        ema_8=204.0,
        ema_21=198.0,
        ema_34=192.0,
        ema_55=185.0,
        ema_89=175.0,
        atr_14=6.0,
        adx_14=24.5,
    )

    state: TradingState = {
        "session_id": "sess-bounce-test",
        "mode": "dry_run",
        "selected_playbook": "momentum_squeeze",
        "target_ticker": None,
        "regime": MarketRegime(posture="BULLISH"),
        "candidates": [],
        "technicals": {"NVDA": tech_nvda},
        "options_audits": {},
        "proposals": [],
        "rejected_proposals": [],
        "execution_results": [],
        "needs_human_approval": False,
        "human_decision": None,
        "audit_trail": [],
        "reflection_notes": [],
        "errors": [],
    }

    out = await playbooks_node(state)
    assert len(out["proposals"]) >= 1

    nvda_prop = next(p for p in out["proposals"] if p.ticker == "NVDA")
    assert nvda_prop.side == "BUY"
    assert nvda_prop.asset_type == "EQUITY"
    assert nvda_prop.stop_loss < 200.0
    assert nvda_prop.profit_target > 200.0
    assert nvda_prop.risk_reward_ratio >= 1.5
