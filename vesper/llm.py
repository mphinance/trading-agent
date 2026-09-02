"""OpenRouter LLM Integration Layer for Vesper.

Provides cost-optimized, low-latency reasoning and thesis synthesis using
OpenRouter models (DeepSeek-V4 Flash / Pro / R1, Gemini 2.5 Flash Lite, Claude 3.5).
Gracefully degrades to deterministic rule engines when running offline or without an API key.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
import httpx

from core.metrics import metrics

logger = logging.getLogger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
REASONING_MODEL = "deepseek/deepseek-r1"
PRO_MODEL = "deepseek/deepseek-v4-pro"
FALLBACK_MODEL = "google/gemini-2.5-flash-lite"
ESCALATION_NOTIONAL_THRESHOLD = float(os.getenv("VESPER_ESCALATION_NOTIONAL", "1000.0"))


def select_audit_model(
    proposal_dict: Dict[str, Any],
    regime_posture: str = "NEUTRAL",
) -> str:
    """Select appropriate model tier for proposal auditing.

    Escalates to PRO_MODEL (deepseek/deepseek-v4-pro) when notional cost >= threshold,
    max risk is elevated (>= $250), or market regime is extreme/volatile.
    Otherwise uses DEFAULT_MODEL (deepseek/deepseek-v4-flash).
    """
    est_cost = float(proposal_dict.get("estimated_cost", 0.0) or 0.0)
    max_risk = float(proposal_dict.get("max_risk", 0.0) or 0.0)
    regime_str = str(regime_posture or "").upper()

    is_escalated = (
        est_cost >= ESCALATION_NOTIONAL_THRESHOLD
        or max_risk >= 250.0
        or regime_str in ("HIGH_VOLATILITY", "BEARISH_EXTREME", "VOLATILE")
    )
    return PRO_MODEL if is_escalated else DEFAULT_MODEL


def is_llm_enabled() -> bool:
    """Return True if an OpenRouter API key is configured."""
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    return bool(key and not key.startswith("your_"))


async def call_openrouter(
    messages: List[Dict[str, str]],
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
    json_mode: bool = False,
    timeout_sec: float = 15.0,
) -> Optional[str]:
    """Call OpenRouter API with async httpx client.

    Args:
        messages: Chat history list of dicts with 'role' and 'content'.
        model: OpenRouter model identifier (e.g. 'deepseek/deepseek-v4-flash').
        temperature: Sampling temperature (0.0 to 1.0).
        json_mode: Force JSON response output format.
        timeout_sec: Maximum request timeout.

    Returns:
        Generated string response, or None if call fails or key is missing.
    """
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key or api_key.startswith("your_"):
        logger.debug("OPENROUTER_API_KEY not set. Skipping LLM call.")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/mphinance/webull-sidecar",
        "X-Title": "Vesper Quant Trading System",
    }

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }

    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.post(OPENROUTER_API_URL, headers=headers, json=payload)
            dt = time.monotonic() - start
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
                    metrics.record_llm_call(model, dt, outcome="json_error")
                    logger.warning(f"OpenRouter response shape/parse error: {e}")
                    return None
                metrics.record_llm_call(model, dt, outcome="ok")
                return content.strip()
            else:
                metrics.record_llm_call(model, dt, outcome="http_error")
                logger.warning(
                    f"OpenRouter API call failed ({resp.status_code}): {resp.text[:200]}"
                )
                return None
    except Exception as e:
        metrics.record_llm_call(model, time.monotonic() - start, outcome="timeout_or_network")
        logger.warning(f"OpenRouter network/request error: {e}")
        return None


async def generate_candidate_thesis(
    ticker: str,
    technical_summary: str,
    candidate_rationale: str,
    regime_posture: str = "NEUTRAL",
    model: str = DEFAULT_MODEL,
) -> Dict[str, Any]:
    """Generate structured AI trade thesis and conviction scoring for a setup candidate."""
    if not is_llm_enabled():
        metrics.record_llm_call(model, 0.0, outcome="disabled")
        return {
            "source": "deterministic_fallback",
            "thesis": candidate_rationale or f"Technical setup on {ticker} in {regime_posture} regime.",
            "confidence": 3,
            "key_catalysts": ["EMA alignment", "Action Zone pullback"],
        }

    system_prompt = (
        "You are Vesper, an elite quantitative equity and options portfolio manager. "
        "Analyze the provided technical setup, market regime, and scanner signals. "
        "Respond STRICTLY with valid JSON containing:\n"
        '{\n'
        '  "thesis": "Concise 2-sentence thesis explaining the edge and invalidation level",\n'
        '  "confidence": <integer from 1 to 5>,\n'
        '  "key_catalysts": ["catalyst 1", "catalyst 2"]\n'
        '}'
    )

    user_prompt = (
        f"Ticker: {ticker}\n"
        f"Market Regime: {regime_posture}\n"
        f"Scanner Signal: {candidate_rationale}\n"
        f"Technicals: {technical_summary}\n"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    raw_resp = await call_openrouter(messages, model=model, temperature=0.1, json_mode=True)
    if not raw_resp:
        return {
            "source": "deterministic_fallback",
            "thesis": candidate_rationale,
            "confidence": 3,
            "key_catalysts": ["Technical setup"],
        }

    try:
        parsed = json.loads(raw_resp)
        parsed["source"] = f"openrouter/{model}"
        return parsed
    except json.JSONDecodeError:
        return {
            "source": f"openrouter/{model}",
            "thesis": raw_resp,
            "confidence": 3,
            "key_catalysts": [],
        }


async def audit_proposal_risk(
    proposal_dict: Dict[str, Any],
    regime_posture: str = "NEUTRAL",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Perform LLM red-team check on an order proposal before execution.

    Uses PRO_MODEL for escalated cases (high notional, elevated risk, volatile regime)
    and DEFAULT_MODEL otherwise.

    Executes a single-round adversarial self-critique follow-up when the initial
    verdict is REDUCE_SIZE, REJECT, or flags a risk_score >= 7, allowing the model
    to confirm or revise its assessment before final gate submission.
    """
    chosen_model = model or select_audit_model(proposal_dict, regime_posture)
    if not is_llm_enabled():
        metrics.record_llm_call(chosen_model, 0.0, outcome="disabled")
        return {"passed": True, "notes": "Risk audit bypassed (deterministic mode active)."}

    system_prompt = (
        "You are a strict risk management officer auditing an algorithmic trade proposal. "
        "Review the trade parameters against current market posture. "
        "Respond with JSON:\n"
        '{\n'
        '  "passed": true|false,\n'
        '  "risk_score": <1-10>,\n'
        '  "concerns": ["concern 1", "concern 2"],\n'
        '  "recommendation": "PROCEED" | "REDUCE_SIZE" | "REJECT"\n'
        '}'
    )

    user_prompt = (
        f"Order Proposal: {json.dumps(proposal_dict)}\n"
        f"Current Market Regime: {regime_posture}\n"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    raw_resp = await call_openrouter(messages, model=chosen_model, temperature=0.1, json_mode=True)
    if not raw_resp:
        return {"passed": True, "notes": "LLM risk audit unavailable, deterministic guards active."}

    try:
        first_verdict = json.loads(raw_resp)
    except Exception:
        return {"passed": True, "notes": raw_resp}

    if not isinstance(first_verdict, dict):
        return {"passed": True, "notes": raw_resp}

    rec = str(first_verdict.get("recommendation", "PROCEED")).upper()
    risk_score = float(first_verdict.get("risk_score", 0)) if isinstance(first_verdict.get("risk_score"), (int, float)) else 0
    passed = bool(first_verdict.get("passed", True))

    # Self-ask / adversarial review loop: single reconsideration turn for elevated risk verdicts
    if (rec in ("REDUCE_SIZE", "REJECT")) or (risk_score >= 7) or (not passed):
        messages.append({"role": "assistant", "content": raw_resp})
        reconsider_prompt = (
            f"Adversarial Self-Review: Your initial verdict was '{rec}' with risk_score={risk_score}. "
            f"Concerns noted: {first_verdict.get('concerns', [])}.\n"
            "Critically evaluate whether the setup's risk-reward, stop loss placement, regime posture, "
            "and sizing mitigation warrant this outcome. Either confirm your verdict or provide a revised verdict. "
            "Respond STRICTLY with valid JSON matching the exact same schema:\n"
            '{\n'
            '  "passed": true|false,\n'
            '  "risk_score": <1-10>,\n'
            '  "concerns": ["concern 1", "concern 2"],\n'
            '  "recommendation": "PROCEED" | "REDUCE_SIZE" | "REJECT"\n'
            '}'
        )
        messages.append({"role": "user", "content": reconsider_prompt})
        revised_raw = await call_openrouter(messages, model=chosen_model, temperature=0.1, json_mode=True)
        if revised_raw:
            try:
                revised_verdict = json.loads(revised_raw)
                if isinstance(revised_verdict, dict):
                    revised_verdict["self_critique_performed"] = True
                    return revised_verdict
            except Exception as e:
                logger.debug("Adversarial review parse error: %s", e)

    return first_verdict
