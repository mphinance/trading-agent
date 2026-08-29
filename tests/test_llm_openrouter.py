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
    """Verify call_openrouter parses API response and handles model routing."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-validkey")

    mock_resp_json = {
        "choices": [
            {
                "message": {
                    "content": '{"thesis": "Strong momentum pullback bounce", "confidence": 5, "key_catalysts": ["EMA cross"]}'
                }
            }
        ]
    }

    mock_post = AsyncMock()
    mock_post.return_value.status_code = 200
    mock_post.return_value.json = lambda: mock_resp_json

    with patch("httpx.AsyncClient.post", mock_post):
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

    with patch("vesper.llm.call_openrouter", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_resp
        res_low = await audit_proposal_risk(low_notional_prop, regime_posture="NEUTRAL")
        assert res_low["recommendation"] == "PROCEED"
        mock_call.assert_called_once()
        assert mock_call.call_args.kwargs["model"] == "deepseek/deepseek-v4-flash"

    # 2. High-notional proposal -> PRO_MODEL
    high_notional_prop = {
        "ticker": "NVDA",
        "quantity": 20,
        "limit_price": 125.0,
        "estimated_cost": 2500.0,
        "max_risk": 350.0,
    }

    with patch("vesper.llm.call_openrouter", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_resp
        res_high = await audit_proposal_risk(high_notional_prop, regime_posture="NEUTRAL")
        assert res_high["recommendation"] == "PROCEED"
        mock_call.assert_called_once()
        assert mock_call.call_args.kwargs["model"] == "deepseek/deepseek-v4-pro"


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

    with patch("vesper.llm.call_openrouter", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = [turn_1_json, turn_2_json]
        final_verdict = await audit_proposal_risk(prop, regime_posture="BULLISH")

        # Verify exactly 2 sequential calls were made (1 follow-up critique round, capped)
        assert mock_call.call_count == 2
        # Verify second call received the first verdict in history and adversarial prompt
        second_call_messages = mock_call.call_args_list[1].args[0]
        assert len(second_call_messages) == 4
        assert "Adversarial Self-Review" in second_call_messages[3]["content"]

        # Verify the second (revised) verdict is the one returned
        assert final_verdict["passed"] is True
        assert final_verdict["risk_score"] == 4
        assert final_verdict["recommendation"] == "PROCEED"
        assert final_verdict.get("self_critique_performed") is True
