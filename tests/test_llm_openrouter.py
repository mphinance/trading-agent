"""Tests for OpenRouter LLM Integration Layer."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch
from vesper.llm import (
    is_llm_enabled,
    call_openrouter,
    generate_candidate_thesis,
    audit_proposal_risk,
)
from core.metrics import metrics
from llm_fakes import DeterministicProvider


def test_is_llm_enabled_detection(monkeypatch):
    """Verify is_llm_enabled correctly inspects environment configuration."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert not is_llm_enabled()

    monkeypatch.setenv("OPENROUTER_API_KEY", "your_api_key_here")
    assert not is_llm_enabled()

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abcdef123456")
    assert is_llm_enabled()


@pytest.mark.asyncio
async def test_deterministic_fallback_when_openrouter_disabled(monkeypatch):
    """Verify fallback provides structured thesis without network calls when disabled."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    res = await generate_candidate_thesis(
        ticker="NVDA",
        technical_summary="RSI=52, EMA stack bullish",
        candidate_rationale="Pullback on 21 EMA",
        regime_posture="BULLISH",
    )

    assert res["source"] == "deterministic_fallback"
    assert "NVDA" in res["thesis"] or "Pullback" in res["thesis"]
    assert res["confidence"] == 3

    risk_audit = await audit_proposal_risk(
        proposal_dict={"ticker": "NVDA", "quantity": 10},
        regime_posture="BULLISH",
    )
    assert risk_audit["passed"] is True


@pytest.mark.asyncio
async def test_call_openrouter_mock_success(monkeypatch):
    """Verify generate_candidate_thesis parses the provider's response and handles model routing."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-validkey")

    provider = DeterministicProvider(
        ['{"thesis": "Strong momentum pullback bounce", "confidence": 5, "key_catalysts": ["EMA cross"]}']
    )
    monkeypatch.setattr("vesper.llm.call_openrouter", provider)

    thesis = await generate_candidate_thesis(
        ticker="TSLA",
        technical_summary="RSI 48, ADX 22",
        candidate_rationale="Squeeze setup",
        regime_posture="BULLISH",
        model="deepseek/deepseek-v4-flash",
    )

    assert "openrouter/deepseek/deepseek-v4-flash" in thesis["source"]
    assert thesis["confidence"] == 5
    assert thesis["thesis"] == "Strong momentum pullback bounce"
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_audit_proposal_model_escalation(monkeypatch):
    """Verify PRO_MODEL is selected for high-notional/high-risk cases and DEFAULT_MODEL otherwise."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-validkey")

    mock_resp = '{"passed": true, "risk_score": 2, "concerns": [], "recommendation": "PROCEED"}'

    # 1. Low-notional proposal -> DEFAULT_MODEL
    low_notional_prop = {
        "ticker": "AAPL",
        "quantity": 2,
        "limit_price": 150.0,
        "estimated_cost": 300.0,
        "max_risk": 50.0,
    }

    provider = DeterministicProvider([mock_resp])
    with patch("vesper.llm.call_openrouter", provider):
        res_low = await audit_proposal_risk(low_notional_prop, regime_posture="NEUTRAL")
        assert res_low["recommendation"] == "PROCEED"
        assert provider.call_count == 1
        assert provider.calls[0]["model"] == "deepseek/deepseek-v4-flash"

    # 2. High-notional proposal -> PRO_MODEL
    high_notional_prop = {
        "ticker": "NVDA",
        "quantity": 20,
        "limit_price": 125.0,
        "estimated_cost": 2500.0,
        "max_risk": 350.0,
    }

    provider = DeterministicProvider([mock_resp])
    with patch("vesper.llm.call_openrouter", provider):
        res_high = await audit_proposal_risk(high_notional_prop, regime_posture="NEUTRAL")
        assert res_high["recommendation"] == "PROCEED"
        assert provider.call_count == 1
        assert provider.calls[0]["model"] == "deepseek/deepseek-v4-pro"


@pytest.mark.asyncio
async def test_audit_proposal_adversarial_review_loop(monkeypatch):
    """Verify two sequential OpenRouter calls occur on cautionary verdicts and revised verdict is used."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-validkey")

    prop = {
        "ticker": "TSLA",
        "quantity": 10,
        "limit_price": 200.0,
        "estimated_cost": 2000.0,
        "max_risk": 400.0,
    }

    # Turn 1: Initial caution (REDUCE_SIZE, risk_score 8)
    turn_1_json = '{"passed": false, "risk_score": 8, "concerns": ["Market turbulence"], "recommendation": "REDUCE_SIZE"}'
    # Turn 2: Revised verdict after reconsidering stop loss & regime
    turn_2_json = '{"passed": true, "risk_score": 4, "concerns": ["Tight trailing stop mitigates turbulence"], "recommendation": "PROCEED"}'

    provider = DeterministicProvider([turn_1_json, turn_2_json])
    with patch("vesper.llm.call_openrouter", provider):
        final_verdict = await audit_proposal_risk(prop, regime_posture="BULLISH")

        # Verify exactly 2 sequential calls were made (1 follow-up critique round, capped)
        assert provider.call_count == 2
        # Verify second call received the first verdict in history and adversarial prompt
        second_call_messages = provider.calls[1]["messages"]
        assert len(second_call_messages) == 4
        assert "Adversarial Self-Review" in second_call_messages[3]["content"]

        # Verify the second (revised) verdict is the one returned
        assert final_verdict["passed"] is True
        assert final_verdict["risk_score"] == 4
        assert final_verdict["recommendation"] == "PROCEED"
        assert final_verdict.get("self_critique_performed") is True


# -- metrics.py instrumentation (record_llm_call) ------------------------------


@pytest.mark.asyncio
async def test_call_openrouter_records_ok_outcome(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-validkey")
    mock_resp = AsyncMock()
    mock_resp.return_value.status_code = 200
    mock_resp.return_value.json = lambda: {"choices": [{"message": {"content": "hi"}}]}
    with patch("httpx.AsyncClient.post", mock_resp):
        await call_openrouter([{"role": "user", "content": "x"}], model="deepseek/deepseek-v4-flash")
    snap = metrics.snapshot()["llm_calls"]["deepseek/deepseek-v4-flash"]
    assert snap["ok"] == 1


@pytest.mark.asyncio
async def test_call_openrouter_records_http_error_outcome(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-validkey")
    mock_resp = AsyncMock()
    mock_resp.return_value.status_code = 500
    mock_resp.return_value.text = "server error"
    with patch("httpx.AsyncClient.post", mock_resp):
        result = await call_openrouter([{"role": "user", "content": "x"}], model="deepseek/deepseek-v4-flash")
    assert result is None
    snap = metrics.snapshot()["llm_calls"]["deepseek/deepseek-v4-flash"]
    assert snap["http_error"] == 1


@pytest.mark.asyncio
async def test_call_openrouter_records_timeout_or_network_outcome(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-validkey")
    with patch("httpx.AsyncClient.post", AsyncMock(side_effect=RuntimeError("connection reset"))):
        result = await call_openrouter([{"role": "user", "content": "x"}], model="deepseek/deepseek-v4-flash")
    assert result is None
    snap = metrics.snapshot()["llm_calls"]["deepseek/deepseek-v4-flash"]
    assert snap["timeout_or_network"] == 1


@pytest.mark.asyncio
async def test_call_openrouter_records_json_error_outcome_on_unparseable_response(monkeypatch):
    """A 200 whose body doesn't have the expected choices/message/content
    shape must count as json_error, not silently as ok."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-validkey")
    mock_resp = AsyncMock()
    mock_resp.return_value.status_code = 200
    mock_resp.return_value.json = lambda: {"unexpected": "shape"}
    with patch("httpx.AsyncClient.post", mock_resp):
        result = await call_openrouter([{"role": "user", "content": "x"}], model="deepseek/deepseek-v4-flash")
    assert result is None
    snap = metrics.snapshot()["llm_calls"]["deepseek/deepseek-v4-flash"]
    assert snap["json_error"] == 1


@pytest.mark.asyncio
async def test_generate_candidate_thesis_disabled_records_disabled_outcome(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from vesper.llm import DEFAULT_MODEL

    await generate_candidate_thesis(
        ticker="NVDA", technical_summary="x", candidate_rationale="y", regime_posture="NEUTRAL",
    )
    snap = metrics.snapshot()["llm_calls"][DEFAULT_MODEL]
    assert snap["disabled"] == 1


@pytest.mark.asyncio
async def test_audit_proposal_risk_disabled_records_disabled_outcome_on_selected_tier(monkeypatch):
    """Disabled-mode outcome is recorded under whichever tier
    select_audit_model would have escalated to -- not always DEFAULT_MODEL --
    so a disabled high-notional proposal is still distinguishable from a
    disabled low-notional one."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from vesper.llm import PRO_MODEL

    high_notional_prop = {"ticker": "NVDA", "quantity": 20, "limit_price": 125.0,
                           "estimated_cost": 2500.0, "max_risk": 350.0}
    res = await audit_proposal_risk(high_notional_prop, regime_posture="NEUTRAL")
    assert res["passed"] is True
    snap = metrics.snapshot()["llm_calls"][PRO_MODEL]
    assert snap["disabled"] == 1


# -- tests/llm_fakes.DeterministicProvider --------------------------------


@pytest.mark.asyncio
async def test_deterministic_provider_returns_queued_responses_in_order():
    """Working path: responses pop off the queue in call order, and every
    call's arguments are recorded for assertion."""
    provider = DeterministicProvider(["first", "second"])

    messages = [{"role": "user", "content": "hi"}]
    r1 = await provider(messages, model="model-a", temperature=0.3, json_mode=True)
    r2 = await provider(messages, model="model-b")

    assert r1 == "first"
    assert r2 == "second"
    assert provider.call_count == 2
    assert provider.calls[0]["model"] == "model-a"
    assert provider.calls[0]["temperature"] == 0.3
    assert provider.calls[0]["json_mode"] is True
    assert provider.calls[1]["model"] == "model-b"


@pytest.mark.asyncio
async def test_deterministic_provider_returns_none_when_queue_exhausted(monkeypatch):
    """Degraded path: an empty (or exhausted) queue returns None, exactly
    like call_openrouter does on a network/parse failure -- so callers that
    already handle call_openrouter returning None need no special-casing
    when swapped over to this fake."""
    provider = DeterministicProvider()
    assert await provider([{"role": "user", "content": "x"}]) is None

    # Wire it in as the module-level call_openrouter and confirm
    # generate_candidate_thesis's existing "no response" fallback fires --
    # it must not fabricate a thesis when the provider yields nothing.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-validkey")
    monkeypatch.setattr("vesper.llm.call_openrouter", provider)

    res = await generate_candidate_thesis(
        ticker="NVDA",
        technical_summary="RSI=52",
        candidate_rationale="Pullback on 21 EMA",
        regime_posture="NEUTRAL",
    )
    assert res["source"] == "deterministic_fallback"
    assert res["thesis"] == "Pullback on 21 EMA"
