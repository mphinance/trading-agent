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
