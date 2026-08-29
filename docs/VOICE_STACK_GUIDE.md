# 🎙️ Vesper Cloud Voice Stack (Zero-GPU Architecture)

This document specifies the decoupled **Cloud STT $\rightarrow$ DeepSeek-V4 Brain $\rightarrow$ Kokoro-82M TTS** pipeline using OpenRouter cloud endpoints without requiring local GPUs.

> **Transport decided 2026-08-29 — read this before implementing the diagram
> below.** The **model layers here are current and were chosen deliberately**
> (STT options, Kokoro `af_heart`, costs). What changed is how audio gets in
> and out: it is a **Telegram voice note**, pulled by the existing
> outbound-only `telegram_polling.py` loop, not a local always-on microphone.
> There is therefore **no wake word** — push-to-talk is by construction — and
> **no audio-upload endpoint**, which is the point: the chosen shape adds no
> listener and inherits the `TELEGRAM_AUTHORIZED_USER_IDS` allowlist.
>
> Two scope constraints go with it: **approvals stay on the inline buttons and
> never become a voice command** (a transcript is ambiguous exactly where it
> must not be), and voice targets **contextual queries rather than naming
> symbols** (ticker mis-transcription is a measured failure here — NVDA → "in
> video"). Box 3's "terminal / UI confirmation" below should be read as the
> Telegram message thread, which is also the required text audit record.
>
> Full reasoning and the alternatives weighed: `ROADMAP.md` → "LLM layer +
> voice". Binding rules: `CLAUDE.md` rule 4d.

---

## 🏛️ Pipeline Architecture

```
                  ┌────────────────────────────────────────────────────────┐
                  │          Decoupled Cloud Voice Pipeline (Zero GPU)     │
                  └───────────────────────────┬────────────────────────────┘
                                              │
                    [ Spoken Voice: "What's the 0DTE SPY gamma flip?" ]
                                              │
                                              ▼
                    ┌───────────────────────────────────────────────────┐
                    │ 1. CLOUD STT (Audio-In -> Text)                   │
                    │    • mistralai/voxtral-small-24b ($0.100/1M)      │
                    │    • or google/gemini-2.5-flash-lite ($0.100/1M)  │
                    │    • Transcribes speech to clean prompt text      │
                    └─────────────────────────┬─────────────────────────┘
                                              │ Prompt Text
                                              ▼
                    ┌───────────────────────────────────────────────────┐
                    │ 2. QUANT BRAIN: deepseek/deepseek-v4-flash        │
                    │    • 1M Token Context                             │
                    │    • Exact GEX math, VoPR options pricing, risk   │
                    │    • Cost: $0.087 / 1M input tokens               │
                    └─────────────────────────┬─────────────────────────┘
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      ▼                                               ▼
    ┌───────────────────────────────────┐           ┌───────────────────────────────────┐
    │ 3. TERMINAL / UI CONFIRMATION     │           │ 4. CLOUD TTS: hexgrad/kokoro-82m  │
    │    Renders exact numbers, price,  │           │    • Endpoint: /v1/audio/speech   │
    │    stop loss, and order card      │           │    • Voice: af_heart (Bella/Heart)│
    │    for sanity verification        │           │    • Cost: $0.62 / 1M characters  │
    └───────────────────────────────────┘           └───────────────────────────────────┘
```

---

## 👂 1. Cloud STT Options on OpenRouter (Audio $\rightarrow$ Text)

| Model ID | Input / 1M Tokens | Output / 1M Tokens | Context Window | Best Suited For |
|---|---|---|---|---|
| 👑 **`mistralai/voxtral-small-24b-2507`** | **$0.100** | **$0.300** | 32,768 | Purpose-built speech transcription and audio understanding. |
| ⚡ **`google/gemini-2.5-flash-lite`** | **$0.100** | **$0.400** | **1,048,576** (1.0M) | Ultra-low latency, handles long recordings & earnings calls. |
| 🌐 **`xiaomi/mimo-v2.5`** | **$0.140** | **$0.280** | **1,050,000** (1.0M) | Fast multimodal audio ingestion. |
| 🎯 **`google/gemini-2.5-flash`** | **$0.300** | **$2.500** | **1,048,576** (1.0M) | High financial term accuracy across noisy microphones. |
| 🎙️ **`openai/gpt-audio-mini`** | **$0.600** | **$2.400** | 128,000 | Native OpenAI transcription compatibility. |

---

## 🗣️ 2. Cloud TTS Option: Kokoro-82M (`af_heart`)

* **Model ID**: `hexgrad/kokoro-82m`
* **Endpoint**: `POST https://openrouter.ai/api/v1/audio/speech`
* **Default Voice**: **`af_heart`** (American English Female, studio grade, clear cadence for stock prices and percentages).
* **Alternative Voices**: `am_adam`, `bf_alice`, `bm_george`.
* **Pricing**: **$0.62 / 1 Million characters** (~$0.0006 per 200-word market briefing).
* **Format**: Streams direct MP3 / WAV audio bytes to client.

---

## 💰 End-to-End Cost per Interaction

| Step | Service | Price per Interaction |
|---|---|---|
| **STT** (Audio $\rightarrow$ Text) | `mistralai/voxtral-small-24b` | **$0.0001** |
| **Brain** (Quant Logic & GEX) | `deepseek/deepseek-v4-flash` | **$0.0003** |
| **TTS** (Voice Out: `af_heart`) | `hexgrad/kokoro-82m` | **$0.0006** |
| **Total Cost per Voice Briefing** | | **~$0.0010** *(1/10th of a cent)* |
