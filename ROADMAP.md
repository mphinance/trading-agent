# 🗺️ Vesper Engine & Broker Integration Master Roadmap

This document is the authoritative roadmap, architectural specification, and operational execution plan for **Vesper**.

See [`docs/TIERS_AND_FUNNEL.md`](docs/TIERS_AND_FUNNEL.md) for the complete **Starter (Dealer-HUD)** vs. **Pro (TDPro MCP + Vesper)** ecosystem architecture, and [`docs/CODE_SWEEP_2026-08-28.md`](docs/CODE_SWEEP_2026-08-28.md) for code audit findings.

---

## 🔌 Broker Integration Matrix

| Broker | Status | Assets Supported | Auth / Config | Notes |
|---|---|---|---|---|
| **Webull OpenAPI** | ✅ **Active** | Stocks, ETFs, Options, Futures, Crypto | `WEBULL_APP_KEY`, `WEBULL_APP_SECRET` | Official OpenAPI SDK, cash/margin support, 91 MCP tools, guarded by `ExecutionGuard`. |
| **Public.com** | 🟡 **Pre-Wired** | Stocks, ETFs, Options, Crypto, Bonds | `PUBLIC_API_SECRET_KEY`, `PUBLIC_ACCOUNT_ID` | Agentic Brokerage API & Hosted MCP (`https://api.public.com`). Ready to activate when key is provided. |
| **Tradier** | ⚪ **Planned** | Equities, Index Options (XSP / SPX) | `TRADIER_API_KEY` | Dedicated low-latency 0DTE route. |
| **Interactive Brokers (IBKR)** | ⚪ **Planned** | Global multi-asset, Forex, Futures | Client Portal Gateway | Safe "Draft-Only" human UI approval mode. |
| **Alpaca** | ⚪ **Planned** | US Equities, Crypto, Multi-leg Options | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Built-in paper trading sandbox. |

---

## ⚠️ Technical Gotchas (Critical Rules)

- **Synchronous Webull SDK in Async Graph**: `wb.py`'s Webull SDK client is synchronous/blocking. Wrap all blocking Webull SDK calls in `asyncio.to_thread(...)` to prevent stalling the async event loop.
- **Webull Account Rate Limits**: Account and order-query endpoints are limited to **2 req / 2s** (separate from the 600 req/min market data bucket). Reuse `wb.py`'s internal caching.
- **Buying Power Sharing**: Webull shared cash/margin accounts share buying power across accounts — use `max()`, not `sum()`.
- **TraderDaddy Pro Conviction Param**: `get_conviction` takes `symbol`, not `ticker`. Force UTF-8 decoding on API responses.
- **Execution Guardrails Default**: `VESPER_TRADING` defaults to `0` (off). Live order execution requires explicit `VESPER_TRADING=1`.

---

## 🎯 Modular Architecture & Execution Roadmap

### ✅ Module 0: Execution Guardrails & Live Equity Rebuild (Completed)
- [x] **Ticket Handshake**: `preview()` stages a hashed, single-use, 60s time-limited ticket; `place()` verifies the payload SHA-256 before firing.
- [x] **Server-Side Caps**: Notional cap (`VESPER_MAX_NOTIONAL_USD`, default $2,500) and quantity cap (`VESPER_MAX_QUANTITY`, default 100 shares / 10 contracts).
- [x] **Symbol Allowlist**: Optional allowlist (`VESPER_ALLOWED_SYMBOLS`).
- [x] **Kill Switch**: `VESPER_TRADING` (default off) checked before any broker call.
- [x] **Live Equity Integration**: `risk_gate_node` reads live Net Liquidation Value (`nlv`) from Webull with safe fallback.
- [x] **Unit Tests**: 11 unit tests in [`tests/test_execution_guard.py`](tests/test_execution_guard.py) (100% green).

---

### 🌅 Module 1: Automated Pre-Market Battle-Plan Runner (`vesper morning`) — ✅ Landed
- [x] **Macro & Market Health Check**:
  - Query TraderDaddy Pro `get_market_health` (0-7 composite score) and macro regime.
- [x] **Dealer Gamma & Apex Levels**:
  - Query TraderDaddy `levels("SPY")` and `levels("QQQ")` for Spot vs. Gamma Flip line, major open interest magnets, and Apex support/resistance levels.
- [x] **Institutional Whale Flows**:
  - Query TickerTrace Pro `get_briefing` for top smart-money ETF accumulation and cross-fund divergences.
- [x] **Game-Plan Output**:
  - Format concise morning watchlist, 0DTE bias (`BULLISH CALL TRIGGER > $768.62` / `BEARISH PUT TRIGGER < $768.62`), and top 5 momentum candidates with 2x leveraged ETF proxies.

---

### 📱 Module 2: Channel-Agnostic Interactive Live Alert Bot — ✅ Landed
- [x] **Channel-Agnostic Bot Engine**:
  - Defined `ApprovalChannel` interface (`send_proposal_card`, `send_execution_result`, `send_alert`) supporting Discord (`vesper/bot/discord_adapter.py`), Telegram (`vesper/bot/telegram_adapter.py`), and custom Webhooks (`vesper/bot/webhook_adapter.py`).
- [x] **Interactive Visual Cards**:
  - Pushes rich trade proposal cards: Ticker, Direction, Quantity, Limit Price, Max Dollar Risk, Target, Stop-Loss, and quantitative thesis.
- [x] **Execution Callbacks & Multiplexing**:
  - `human_gate_node` and `executor_node` broadcast proposals and execution reports across all configured channels via `ChannelManager`.
- [x] **Unit Tests**:
  - 8 unit tests in [`tests/test_bot_channel.py`](tests/test_bot_channel.py) (100% green).

---

### 🛡️ Module 3: Active Position Monitor & 0DTE Exit Cascade Loop — ✅ Landed
- [x] **Continuous Position Poller**:
  - Background loop (`python vesper.py monitor [--interval 15] [--live] [--once]`) tracking Webull positions within the 2 req / 2s budget.
- [x] **Exit Cascade Rules**:
  - **Hard Take-Profit**: At **+50%** gain, submits sell order for position.
  - **Hard Stop-Loss**: At **-40%** drawdown, immediately triggers emergency stop liquidation.
  - **Trailing Breakeven Lock**: At **+25%**, automatically locks stop-loss to entry price ($0.00 risk).
  - **Time-Based Exit (3:00 PM ET)**: Automatically liquidates 0DTE contracts before final theta/gamma collapse.
  - **Dealer-Gamma Crossing**: Liquidates SPY long calls if spot price breaks below dynamic TraderDaddy Gamma Flip.
- [x] **Unit Tests**:
  - 7 unit tests in [`tests/test_monitor.py`](tests/test_monitor.py) (100% green).

---

### 🧪 Module 4: Walk-Forward Strategy Backtester & Parameter Optimizer
- [ ] **Strategy Presets**:
  - Squeeze Breakout + 8/21 EMA pullback.
  - 0DTE Spot vs. Gamma Flip intraday breakout.
  - High Realized Volatility vs. Implied Volatility (VoPR™ VRP Harvest).
- [ ] **Metrics Generated**:
  - Win Rate (%), Profit Factor, Sharpe Ratio, Max Drawdown (%), Expectancy ($).
- [ ] **Walk-Forward Validation**:
  - In-sample training (2020–2023) vs. out-of-sample validation (2024–2026) to prevent overfitting.

---

### 🧠 Module 5: Outcome Memory — Winners, Losers, and the Ones We Didn't Take
Full design in [`docs/OUTCOME_MEMORY_PLAN.md`](docs/OUTCOME_MEMORY_PLAN.md) — most
of this already exists (`mcp_server/conviction.py`'s log/resolve/scorecard,
already wired from `reflection_node`; `mcp_server/knowledge.py`'s persistent
ChromaDB at `data/chromadb/`). This is fix-and-extend, not build-from-scratch.

**ChromaDB decision**: keep the structured journal (JSON today, sqlite if
volume ever demands it) for win-rate filtering/aggregation — that's what it's
good at, and `conviction.py`'s own docstring already made this call. Add a new
`trade_memory` collection to the **existing** Chroma client (not a second
database) for semantic "have I seen a setup like this before" recall over
free-text thesis/reasoning. Structured facts live as metadata on the vector
entry so a semantic hit can still be filtered by outcome.

- [ ] **Phase 1 — fix the foundation.** Find and kill the duplicate-entry bug
      in `reflection_node`/`log_conviction` (live journal already has exact
      dupes — likely a LangGraph replay, not confirmed). Fix `reflection_node`
      to read `side` off the matched `OrderProposal` instead of guessing
      direction from a substring in `res.message` (currently mislabels every
      rejected/blocked BUY as bearish). Add `origin`/`playbook`/
      `regime_posture`/`session_id` fields to the journal entry shape.
- [ ] **Phase 2 — log what we didn't take.** `risk_gate_node` logs a rejected
      proposal (`origin=REJECTED_BY_RISK_GATE`, `not_taken_reason=err`)
      instead of silently dropping it. Same for `REJECTED_BY_USER`. Candidates
      that never became a proposal at all (`NOT_PROPOSED`) come last — it's
      the expensive one, needs a lighter-weight log path than a full
      `log_conviction()` price-fetch per declined candidate.
- [ ] **Phase 3 — close the resolution loop automatically.** Nothing in
      `vesper/` calls `resolve_convictions()` today; it only runs if someone
      asks a chat agent. Call it from `reflection_node` or a scheduled tick.
- [ ] **Phase 4 — semantic layer.** `ingest_outcome()` reusing
      `knowledge.py`'s `_get_chroma()`/`_embed_texts()` against the new
      `trade_memory` collection; `recall_similar_setups(thesis_text)` for
      `playbooks_node` to query before drafting, and as an MCP tool mirroring
      `search_knowledge()`.
- [ ] **Phase 5 — feed it back into scoring.** Adjust `Candidate.score` using
      resolved hit-rate of similar past setups / the playbook's own win rate.
      This is the step that changes behavior, not just remembers it.

Open decisions (not mine): resolution horizons for a 0DTE rejected signal
(1/5/10 days doesn't fit same-day), whether declined-candidate logging needs
trimming for volume, and whether Phase 5 just nudges `Candidate.score` or
actually deprioritizes a losing playbook outright.

---

### 📈 Module 6: Leveraged ETF Proxy & Skills-Library Scanner Integration — ✅ Landed
- [x] **Leveraged Proxy Surfacing**:
  - Automatically queries `data/leveraged_etfs.db` in `playbooks_node` to emit risk-scaled 2x high-beta alternates (e.g. `NVDA` ➔ `NVDU`, `TSLA` ➔ `TSLL`, `MSTR` ➔ `MSTX`, `AVGO` ➔ `AVGX`).
- [x] **Skills as Candidate Sources**:
  - Integrated `mcp_server/vcp_screener`, `mcp_server/screener`, and TickerTrace institutional flows into `scanner_node`.
- [x] **Autonomous Skill Evolution**:
  - Built autonomous skill creation and path-traversal validated authoring engine in [`vesper/skills_engine.py`](vesper/skills_engine.py).
- [x] **Unit Tests**:
  - 3 unit tests in [`tests/test_leveraged_and_skills.py`](tests/test_leveraged_and_skills.py) (100% green).

---

### 🧯 Module 7: Paper P&L Ledger & Remote Kill Switch
- [ ] **Paper P&L Ledger**:
  - Append every simulated fill to `data/paper_ledger.json` and mark to market daily.
- [ ] **Remote CLI & Chat Kill Switch**:
  - Implement `vesper halt` and `/halt` bot command for instant remote execution freezing.

---

### 💡 Ideas Backlog (speculative, unscoped — not committed like Modules 0-7)
Full writeup in [`docs/IDEAS_BACKLOG.md`](docs/IDEAS_BACKLOG.md), sourced from
a web-research pass plus two of Michael's NotebookLM notebooks (*Python
Automated Options Wheel Strategy and TradingView Screening*, *AI Trading and
Sentiment Analysis Guide 2026*). Highlights:

- **LLM-as-Bayesian-network-builder for explainable proposals** — an arXiv
  paper on the options wheel strategy has the LLM build a causal DAG (not
  compute probabilities itself); a deterministic engine populates conditional
  probability tables from historical data and does inference. ~27 recorded,
  auditable decision factors per trade. Natural extension of Module 5 once
  there's enough resolved outcome data to populate real CPTs.
- **IntellAgent-style automated evaluation** for `human_gate_node`/any future
  chat layer — generate test conversations via a policy graph + random walk,
  run an automated user agent against a symbolic mock DB, audit with a
  Dialogue Critique agent. Worth it once Module 2's callback receiver exists.
- **Vol-targeting position sizing** (size ∝ target_vol / realized_vol,
  `RiskEnforcer.calculate_equity_size` already has ATR to do this) — best
  effort-to-impact ratio of anything in the backlog, do this one first.
- **Holly (Trade Ideas AI)'s nightly re-optimization**: re-backtest strategy
  variants nightly, only activate ones clearing a win-rate/reward:risk bar
  for the next session — ties Module 4 and Module 5 into an actual gate on
  what `scanner_node` runs, not just a score after the fact.
- Also flagged: risk-segmented playbook "personas" and hedge-vs-directional
  options flow classification.

**Follow-up pass** queried four more notebooks (IBKR API guide, financial
data/infra reference, MCP server directory, and Michael's own trading-
strategy notes). The standout: **`momentum_squeeze` is coded as a breakout
filter, but Michael's actual documented strategy explicitly trades mean-
reversion pullbacks instead** (Keltner "Action Zone" + RSI(2) reset, not
EMA-stack breakout) — see `docs/IDEAS_BACKLOG.md` for the exact rule set.
That same notebook describes four whole strategies with zero code today (an
ADX/IV option-style router, a premium-recycling "free share" engine, a
delta-neutral "Thega" volatility harvest, and a YieldMax `$ULTY` collar-
following play) plus portfolio-level risk rules (15% trailing NLV stop,
underlying-price-keyed option stops, capital-allocation buckets) not in
`vesper/risk.py`. The IBKR pass surfaced concrete, load-bearing gotchas
(single-session-per-account lockout, weekly forced 2FA reset, silent
`CLOSE_WAIT` socket rot, no open/close semantics) for whenever Phase 4's
`ibkr_broker.py` gets built. Full detail, plus named MCP servers worth
connecting (SEC EDGAR, ShareSeer, Polymarket) and data-API notes, in
`docs/IDEAS_BACKLOG.md`.
