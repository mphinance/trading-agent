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


def _state_for(tech: TechnicalAudit) -> TradingState:
    return {
        "session_id": "sess-bounce-test",
        "mode": "dry_run",
        "selected_playbook": "momentum_squeeze",
        "target_ticker": None,
        "regime": MarketRegime(posture="BULLISH"),
        "candidates": [],
        "technicals": {tech.ticker: tech},
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


def _bounce_ready_tech(**overrides) -> TechnicalAudit:
    """A NVDA reading that satisfies all six Bounce 2.0 rules: bullish EMA
    stack, ADX>=18, price inside the Keltner Action Zone, Slow Stochastic<=40,
    an RSI(2) dip-then-reset (8 -> 12, crossed back above 10), RSI(14)<=68."""
    defaults = dict(
        ticker="NVDA",
        close=198.5,          # inside [keltner_lower=195, ema_21+1.5*atr=207]
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
        slow_k=32.0,
        slow_d=30.0,
        rsi_2=12.0,
        rsi_2_prev=8.0,
        keltner_lower=195.0,
        keltner_upper=201.0,
    )
    defaults.update(overrides)
    return TechnicalAudit(**defaults)


@pytest.mark.asyncio
async def test_playbooks_node_synthesizes_bounce_2_proposal():
    """All six Bounce 2.0 rules satisfied -> a proposal gets drafted."""
    out = await playbooks_node(_state_for(_bounce_ready_tech()))
    assert len(out["proposals"]) >= 1

    nvda_prop = next(p for p in out["proposals"] if p.ticker == "NVDA")
    assert nvda_prop.side == "BUY"
    assert nvda_prop.asset_type == "EQUITY"
    assert nvda_prop.stop_loss < 198.5
    assert nvda_prop.profit_target > 198.5
    assert nvda_prop.risk_reward_ratio >= 1.5


@pytest.mark.asyncio
async def test_bounce_2_rejects_outside_action_zone_even_with_bullish_rsi():
    """Regression test: an earlier version of this playbook OR'd the action-
    zone requirement against a bare `rsi_14 > 45`, so almost any mildly
    bullish reading drafted a proposal regardless of whether price had
    actually pulled back. Price here is far above the Action Zone (no
    pullback at all) with an RSI(14) that would have tripped the old
    loophole -- must NOT draft."""
    tech = _bounce_ready_tech(close=230.0, rsi_14=60.0)  # nowhere near keltner_upper=201
    out = await playbooks_node(_state_for(tech))
    assert not any(p.ticker == "NVDA" for p in out["proposals"])


@pytest.mark.asyncio
async def test_bounce_2_rejects_without_rsi_2_dip_reset():
    """In the Action Zone with everything else satisfied, but RSI(2) never
    dipped to <=10 (no oversold reset) -- the entry trigger didn't fire."""
    tech = _bounce_ready_tech(rsi_2=35.0, rsi_2_prev=40.0)
    out = await playbooks_node(_state_for(tech))
    assert not any(p.ticker == "NVDA" for p in out["proposals"])


@pytest.mark.asyncio
async def test_bounce_2_rejects_without_slow_stochastic_data():
    """Missing slow_k must mean 'didn't clear the filter', not 'assume it
    passed' -- this is candidate generation, and a proposal Vesper can't
    actually justify from real data isn't worth showing a human."""
    tech = _bounce_ready_tech(slow_k=None, slow_d=None)
    out = await playbooks_node(_state_for(tech))
    assert not any(p.ticker == "NVDA" for p in out["proposals"])


@pytest.mark.asyncio
async def test_bounce_2_rejects_stochastic_above_40():
    """Slow Stochastic(8,3) must be <= 40 (the documented threshold) -- not
    the looser 45, and not bypassable via a low RSI(14) reading."""
    tech = _bounce_ready_tech(slow_k=44.0, rsi_14=50.0)
    out = await playbooks_node(_state_for(tech))
    assert not any(p.ticker == "NVDA" for p in out["proposals"])
