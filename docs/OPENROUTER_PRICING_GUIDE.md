# 📊 OpenRouter AI Model & Pricing Benchmark (August 2026)

Live comparison pulled directly from `https://openrouter.ai/api/v1/models` to guide cost-optimized model selection for **Vesper** and quantitative agent workflows.

---

## ⚡ Direct Model Comparison Matrix

| Model Tier | OpenRouter Model ID | Input / 1M Tokens | Output / 1M Tokens | Context Window | Best Suited For |
|---|---|---|---|---|---|
| 👑 **Ultra-Low Cost Leader** | `deepseek/deepseek-v4-flash` | **$0.030** | **$0.100** | **1,310,720** (1.3M) | Continuous background screening, high-frequency market polling, bulk technical parsing. |
| ⚡ **Fast Multimodal** | `google/gemini-2.5-flash-lite` | **$0.100** | **$0.400** | **1,048,576** (1.0M) | Rapid chart vision, pre-market news extraction. |
| 🧠 **Balanced Workhorse** | `deepseek/deepseek-chat` (V3) | **$0.257** | **$1.029** | 163,840 | Standard agentic reasoning, playbook decision trees. |
| 🔬 **Complex Quant Reasoning** | `deepseek/deepseek-r1` | **$0.700** | **$2.500** | 163,840 | Mathematical spread pricing, complex options structuring, risk stress testing. |
| 🎯 **Frontier Intelligence** | `deepseek/deepseek-v4-pro` | **$0.742** | **$1.483** | **1,048,576** (1.0M) | Deep multi-step quantitative analysis, thesis generation. |
| 🏢 **Legacy OpenAI Benchmark** | `openai/gpt-4o` | **$2.500** | **$10.000** | 128,000 | General benchmark (80x more expensive than DeepSeek V4 Flash). |
| 💎 **Frontier Coding / Agentic** | `anthropic/claude-3.5-sonnet` | **$3.000** | **$15.000** | 200,000 | Heavy codebase refactoring and interactive pair-programming. |

---

## 💡 Why DeepSeek-V4 is the Clear Winner for Trading Agents

1. **Unbeatable Cost-to-Context Ratio**:
   - **$0.030 / 1M tokens** with a **1.31 Million Token Context Window**.
   - You can feed an entire day's worth of 1-minute order book footprints, SEC 10-Q filings, and institutional ETF flows into a single prompt for **less than half a cent ($0.003)**.
2. **Comparison vs. Claude 3.5 Sonnet & GPT-4o**:
   - **100x Cheaper** than Claude 3.5 Sonnet on input tokens ($0.03 vs. $3.00).
   - **150x Cheaper** on output tokens ($0.10 vs. $15.00).
   - Running a continuous 24/7 scanning loop on GPT-4o would cost ~$50–$100/month in tokens; on **DeepSeek-V4 Flash**, the exact same loop costs **under $1.00/month**.

---

## ⚙️ Setup

`vesper/llm.py` is the integration point — `generate_candidate_thesis()` (used
today, in `playbooks_node`, to attach a narrative/confidence to an
already-fully-constructed proposal — it cannot change quantity, price, or
side) and `audit_proposal_risk()` (written, not yet called from anywhere).
Both degrade to a deterministic fallback with no exception raised if no key
is configured — this is optional enrichment, never a dependency for Vesper to
run.

Add to `.env`:
```bash
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENROUTER_MODEL=deepseek/deepseek-v4-flash  # optional, this is already the default
```

```python
from vesper.llm import generate_candidate_thesis, is_llm_enabled

if is_llm_enabled():
    thesis = await generate_candidate_thesis(
        ticker="NVDA",
        technical_summary="RSI=52, EMA stack bullish, Action Zone pullback",
        candidate_rationale="Tao of Trading Bounce 2.0",
        regime_posture="BULLISH",
    )
```

Verify: `.venv/bin/python -m pytest tests/test_llm_openrouter.py -q`

---

## 🔌 Context7 MCP Integration

**Context7** (`@upstash/context7-mcp`) has been added to our MCP configurations across Antigravity, Claude Code, and Claude Desktop:
- Injects version-specific, real-time SDK and library documentation into the agent prompt.
- Prevents API hallucination when writing LangGraph, Webull OpenAPI, and TraderDaddy Pro integrations.
