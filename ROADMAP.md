# 🗺️ Vesper Roadmap

The single planning doc for Vesper: what's done, what's next, known gaps, and
the ideas backlog. See [`docs/TIERS_AND_FUNNEL.md`](docs/TIERS_AND_FUNNEL.md)
for the Starter (Dealer-HUD) vs. Pro (TDPro MCP + Vesper) ecosystem split.

---

## 🔌 Broker Integration Matrix

| Broker | Status | Assets Supported | Auth / Config | Notes |
|---|---|---|---|---|
| **Webull OpenAPI** | ✅ Active | Stocks, ETFs, Options, Futures, Crypto | `WEBULL_APP_KEY`, `WEBULL_APP_SECRET` | Official OpenAPI SDK, cash/margin, guarded by `ExecutionGuard`. |
| **Public.com** | 🟡 Pre-wired | Stocks, ETFs, Options, Crypto, Bonds | `PUBLIC_API_SECRET_KEY`, `PUBLIC_ACCOUNT_ID` | Guarded, but has no live buying-power lookup yet — see Known Gaps. |
| **Tradier** | ⚪ Planned | Equities, Index Options (XSP/SPX) | `TRADIER_API_KEY` | Dedicated low-latency 0DTE route. |
| **Interactive Brokers (IBKR)** | ⚪ Planned | Global multi-asset, Forex, Futures | Client Portal Gateway | See Module 4's IBKR gotchas below before building this. |
| **Alpaca** | ⚪ Planned | US Equities, Crypto, Multi-leg Options | `APCA_API_KEY_ID`/`SECRET` | Built-in paper sandbox. |

---

## ⚠️ Technical Gotchas

- **Sync Webull SDK in an async graph.** `wb.py` and `PublicBrokerClient` are
  synchronous; every blocking call inside a LangGraph node must go through
  `asyncio.to_thread(...)` or it stalls the event loop for the duration of
  the network round-trip.
- **Webull account/order-query rate limit is 2 req/2s** — separate from the
  600 req/min market-data bucket. Reuse `wb.py`'s internal cache/backoff
  rather than re-polling around it.
- **Buying power is shared across Webull accounts** — `max()`, not `sum()`.
- **TraderDaddy Pro's `get_conviction` takes `symbol`, not `ticker`** —
  wrong key is silently ignored, not an error. Force UTF-8 decoding on
  responses (no charset header, defaults to ISO-8859-1 and mangles em-dashes).
- **`VESPER_TRADING` defaults to `0` (off).** Live order execution needs it
  explicitly set to `1`. Guard config, matching `vesper/execution_guard.py`:
  `VESPER_MAX_NOTIONAL` (default $2,500), `VESPER_MAX_QUANTITY` (default 25),
  `VESPER_MAX_BP_FRACTION` (default 1.0, disabled), `VESPER_SYMBOL_ALLOWLIST`
  (optional). Ticket TTL is 120s.
- **`sk-ant-oat…` is an OAuth token, not an API key** — goes in
  `CLAUDE_CODE_OAUTH_TOKEN`, not `ANTHROPIC_API_KEY`.

---

## 🚧 Known Gaps (not yet a Module, but tracked)

- **Vesper has no LLM reasoning layer anywhere.** Grepped every file in
  `vesper/nodes/` for OpenAI/Anthropic/OpenRouter/`claude_agent_sdk` —
  nothing. `playbooks_node`, `analyst_node`, `regime_node`, `scanner_node`
  are all deterministic Python over TraderDaddy/TickerTrace/technicals data.
  This is worth stating plainly: **the entire reason this is a LangGraph app
  instead of a script is to be an agent, and right now nothing in it actually
  reasons with a model.** See "LLM layer + voice" below for the concrete plan
  once this is ready to build (not yet — notes only, nothing installed).
- **Callback receiver for the Telegram/Discord Approve button — landed.**
  `vesper/bot/inbound.py`'s `approval_registry` + `human_gate_node` reading
  `approval_registry.get_decision(p.id)` before falling through to
  `interrupt()`. Verified it resumes the graph's normal decision flow rather
  than calling `executor_node`/a broker directly — the guard is intact.
- **`PublicBrokerClient` has no live buying-power lookup.** `_execute_public`
  in `executor.py` passes `live_buying_power=None`, so `VESPER_MAX_BP_FRACTION`
  is a no-op on that branch — notional/quantity/allowlist/kill-switch still
  apply. Wire `pub.get_portfolio()` into a real figure to close this.
- **No node-level integration test.** `tests/test_execution_guard.py` proves
  the guard module itself is correct; nothing exercises `executor_node`/
  `risk_gate_node`'s actual wiring against a mocked broker end-to-end.
- **`--persona traderlady` (`vesper.py`) is parsed but never plumbed into
  session state or any node** — dead flag.
- **`vesper/whop.py` (Whop licensing client) is never imported anywhere** —
  not actually integrated despite the commit that introduced it.
- **`requirements.txt` predates the Vesper migration** — missing `langgraph`,
  `pydantic`, `python-dotenv`, `typing_extensions`, which `vesper/` actually
  imports. A clean install can't run `vesper.py`.
- **`vesper/morning.py` silently falls back to hardcoded placeholder SPY/QQQ
  levels** if TraderDaddy is unconfigured or the fetch fails, with no
  "STALE"/"UNAVAILABLE" label distinguishing a real number from a fallback
  one. Read-only (no money moves) but the same failure mode the old
  `alerts.py` was built to prevent: a fabricated number that looks real.

---

## 🎯 Modules

### ✅ Module 0 — Execution Guardrails & Live Equity (done)
- Ticket handshake: `preview()` stages a hashed, single-use, 120s ticket;
  `place()` re-hashes the given payload and refuses to fire on a mismatch.
- Server-side caps (see Gotchas above for current defaults), checked against
  **live** Webull buying power via `wb.portfolio()`.
- Kill switch (`VESPER_TRADING`, default off) checked before any broker call.
- `risk_gate_node` reads live NLV via `vesper/account.py` instead of a
  hardcoded equity constant (same fix applied in `playbooks_node`'s sizing,
  which had an independent copy of the same bug).
- 11 tests in `tests/test_execution_guard.py`.

### ✅ Module 1 — Pre-Market Battle-Plan Runner (`vesper morning`) (done)
Macro/market-health check, SPY/QQQ dealer-gamma levels, TickerTrace whale-flow
briefing, 0DTE bias, top candidates with 2x leveraged-ETF proxies. See Known
Gaps above for the stale-fallback issue.

### ✅ Module 2 — Channel-Agnostic Alert Bot & Inbound Approval Engine (done)
`ApprovalChannel` interface with Telegram/Discord/webhook adapters, broadcast
from `human_gate_node`/`executor_node`. Inbound approval engine in `vesper/bot/inbound.py`
resolves pending proposals, handles Telegram/Discord callback queries and `/halt` commands,
and resumes LangGraph interrupt execution.

### ✅ Module 3 — Position Monitor & Exit Cascade (done)
`vesper monitor [--interval 15] [--live] [--once]`: take-profit +50%,
stop-loss -40%, trailing breakeven +25%, 0DTE time-stop 3:00 PM ET,
dealer-gamma-flip crossing exit for SPY calls. Goes through
`execution_guard` on the live path, same as `executor_node`.

### ✅ Module 4 — Walk-Forward Backtester & Strategy Library (done)
- [x] Strategy presets: `bounce_2_pullback` (Tao of Trading 8/21 EMA pullback into Action Zone),
      `ema_crossover`, `rsi_bounce`, `macd_momentum`, `bollinger_squeeze`, `vopr_vrp_harvest`.
- [x] Metrics: win rate, profit factor, Sharpe, Sortino, max drawdown, expectancy, CAGR, tearsheet generation.
- [x] Walk-forward validation: `walk_forward_test()` with $K$-fold in-sample/out-of-sample windowing and consistency metrics.
- [x] Universe sweeping: `sweep_strategy()` across candidate stock lists. Tested in `tests/test_backtest.py`.

### ✅ Module 5 — Outcome Memory (winners, losers, and the ones we didn't take) (done)
Most of the plumbing already exists — this is fix-and-extend, not
build-from-scratch: `mcp_server/conviction.py` already has a working
log/resolve/scorecard journal (1/5/10-day resolution, WIN/LOSS/PUSH scoring),
already wired from `reflection_node`. `mcp_server/knowledge.py` already runs a
persistent ChromaDB client at `data/chromadb/` with Gemini embeddings for a
139-book knowledge RAG.

**ChromaDB decision**: keep the structured journal (JSON today, sqlite if
volume ever demands it) for win-rate filtering/aggregation — `conviction.py`'s
own docstring already made this call, and a vector DB doesn't make `GROUP BY
playbook` easier. Add a `trade_memory` collection to the **existing** Chroma
client (not a second database) for semantic "have I seen a setup like this
before" recall over free-text thesis — structured facts ride along as
metadata so a semantic hit can still be filtered by outcome.

- [x] **Phase 1 — fix the foundation.** Live `data/conviction_journal.json`
      deduplicated and protected against collisions with microsecond UUID keys.
      `reflection_node` direction mapping fixed to inspect `OrderProposal.side`
      rather than fragile message string heuristics. Added `origin`, `playbook`,
      `regime_posture`, `session_id`, `not_taken_reason`, `target_price`, `stop_loss`.
- [x] **Phase 2 — log what we didn't take.** `risk_gate_node` and `human_gate_node`
      route non-executed/rejected proposals into reflection (`origin=REJECTED_BY_RISK_GATE`,
      `origin=REJECTED_BY_USER`). Screened candidates not synthesized into proposals
      are logged with lightweight price overrides (`origin=NOT_PROPOSED`).
- [x] **Phase 3 — close the resolution loop automatically.** `resolve_convictions()`
      is automatically executed at each session reflection tick to score open
      convictions across 1d/5d/10d horizons.
- [x] **Phase 4 — semantic layer.** `ingest_trade_memory()` stores structured outcome
      vectors into ChromaDB `trade_memory` collection with robust fallback embedding;
      `recall_similar_setups()` enables semantic setup recall and outcome filtering.
- [x] **Phase 5 — feed it back into scoring.** `get_playbook_performance()` calculates
      resolved win rates and calibration adjustments, dynamically scaling proposal
      sizing and conviction in `playbooks_node`.

### ✅ Module 6 — Leveraged ETF Proxy & Skills Library (done)
`playbooks_node` queries `data/leveraged_etfs.db` for risk-scaled 2x
alternates (fetches a **real quote** for the proxy ticker via `md.Market` —
an earlier version reused the underlying's price, which was wrong and fed a
fabricated number into the notional-cap guard; fixed). `scanner_node`
integrates `mcp_server/vcp_screener`/`screener` and TickerTrace flows.
`vesper/skills_engine.py` does path-traversal-validated autonomous skill
authoring. 5 tests in `tests/test_leveraged_and_skills.py`.

### ✅ Module 7 — Paper Ledger & Remote Kill Switch (done)
- [x] Append every simulated fill to `data/paper_ledger.json`, mark to
      market daily with `vesper/paper_ledger.py` and `vesper paper --mark`.
- [x] `vesper halt` CLI + `vesper resume` + `/halt` bot command for instant
      remote freezing, integrated into `ExecutionGuard` and CLI telemetry.

---

## 💡 Ideas Backlog (speculative — not committed, no phase number)

### ⭐ Do first: gaps against Michael's own documented strategy
Queried Michael's own NotebookLM strategy notes (heavily Simon Ree's *Tao of
Trading*) against the three live playbooks. Highest-signal finding of any
research pass done on this repo:

- **`momentum_squeeze` is coded backwards.** It currently drafts on
  `tech.ema_stack == "BULLISH" or tech.rsi_14 > 50` — a breakout filter.
  Michael's actual rule explicitly avoids breakouts as false-breakout traps
  and trades mean-reversion pullbacks ("Bounce 2.0") instead: EMA stack
  `8>21>34>55>89` **and** `ADX(13)≥20`, price pulled back into the Keltner
  "Action Zone" (±1 ATR of the 21 EMA, length 14, 2x multiplier), Slow
  Stochastic(8,3) ≤ 40, entry on `RSI(2)` dipping below 10 then crossing back
  above, confirmed by a close above the pullback's low candle. Exit 50% at
  +2 ATR, 25% at +3 ATR. Precise enough to encode directly — either rewrite
  `momentum_squeeze` to match, or rename the current logic so the two don't
  get confused.
- **Four strategies with zero code**: an ADX/IV option-style router
  (`ADX<20`+`IV≥70%`→Wheel, `ADX≥20`+`IV<70%`→LEAPS, `ADX≥20`+`IV≥70%`→
  Synthetic long via same-strike call+put, else buy shares outright);
  a premium-recycling "free share" engine (sweep 100% of options-selling
  P&L into shares until a free 100-share block accumulates); a delta-neutral
  "Thega" volatility harvest for high-IV binary events (100 shares + 1 ATM
  covered call + 3 ATM CSPs, net delta ≈0); and a YieldMax `$ULTY`
  collar-following play (parse their daily holdings file for the put strikes
  they bought, sell CSPs at those same strikes).
- **`0dte_flow` tightening**: only run weeklies where IV>70%, sell puts at
  0.30 delta or at major OI put walls, reject wide-spread chains, harvest ATM
  CSP vega on earnings week and BTC the next day.
- **Portfolio risk rules missing from `vesper/risk.py`**: a 15% trailing
  peak-to-trough NLV stop that liquidates everything and pauses trading 24h;
  swing-option stops keyed to the *underlying's* price level (200 SMA/34 EMA/
  lower Keltner band) instead of a fixed % on the contract itself; capital
  buckets (15% sector swings, 15% equity options with max one open long
  position, 20% wheel-stock); route 25% of high-yield distributions to `$SGOV`
  for taxes automatically.

### AI agent architecture
- **LLM-as-Bayesian-network-builder for explainable proposals.** An arXiv
  paper on the options wheel strategy has the LLM build a causal DAG (Market
  Regime → Strike Selection → Assignment Probability) instead of computing
  probabilities itself; a deterministic engine (`pgmpy`) populates
  conditional probability tables from historical data and does the inference.
  Their result: 15.3% annualized, Sharpe 1.08 vs 0.62 benchmark, -8.2% max
  drawdown vs -60%, ~27 recorded auditable decision factors per trade.
  Natural extension of Module 5 once there's enough resolved outcome data to
  populate real CPTs instead of priors.
- **IntellAgent-style automated evaluation** for `human_gate_node`/any future
  chat layer: build a policy graph (nodes = rules, edges = co-occurrence
  likelihood), weighted random walk to generate test scenarios at a target
  difficulty, an automated user agent converses against a symbolic mock DB, a
  separate Dialogue Critique agent audits the transcript. Worth it once
  Module 2's callback receiver exists.
- **Risk-scaled guardrail intensity**: scale `execution_guard.py`'s caps/TTL
  to context — tighter when `regime.health_score` is low, looser for
  playbooks with a proven Module-5 track record.
- Graph-structured memory (Mem0/Cognee-style) is the rung *past* Module 5's
  vector-plus-structured design, for if `trade_memory` ever needs
  relationship questions ("what's correlated with what") similarity search
  can't answer. Not now.

### Trading-specific
- **Vol-targeting position sizing — best effort:impact ratio here, do this
  one first.** `RiskEnforcer.calculate_equity_size` already computes ATR;
  size ∝ `target_vol / realized_vol` is a formula change, not new infra.
- **Holly (Trade Ideas AI)'s nightly re-optimization**: re-backtest strategy
  variants nightly, only activate ones clearing a win-rate/reward:risk bar
  for the next session. Ties Module 4 and Module 5 into an actual gate on
  what `scanner_node` runs tomorrow, not just a score after the fact. Also
  worth stealing: risk-segmented playbook "personas" (maps onto the existing
  `--playbook` flag) and continuous streaming over interval polling.
- **Hedge-vs-directional options flow classification**: score whether a
  large print is a directional bet or a dealer/institutional hedge (trade
  size vs. OI, IV skew, proximity to gamma flip) before it becomes a
  `Candidate` — a layer on TraderDaddy's existing flow data, not a new source.
- **Vanna/Charm exposure (VEX/CHEX)**: same conceptual family as the dealer
  gamma (GEX) already tracked, just 2nd/3rd-order. Check whether TraderDaddy
  already exposes these before building anything new.
- Tooling worth knowing about: `vectorbt` (Module 4 candidate), `quantstats`
  (Sharpe/Sortino/drawdown tearsheets + Monte Carlo bust-probability sim for
  the approval card), `py_vollib` (fast Black-Scholes IV/Greeks if options
  math needs to move in-process).

### IBKR integration gotchas (for whenever `ibkr_broker.py` gets built)
- **One login session per account, globally** — TWS/Gateway/Client Portal/
  mobile all fight over one slot. Create a **separate IBKR user** for the bot
  (no withdrawal rights, IP-restricted); never share credentials with the
  account you check manually.
- **Weekly forced session reset** (Sat night/Sun morning). Paper accounts can
  auto-restart cleanly (`ColdRestartTime`); **live accounts need a human to
  ack 2FA on the IBKey app at least once a week** — can't be fully unattended.
- Configure Gateway for **auto-restart, not auto-logoff**, or 2FA is needed
  daily instead of weekly.
- **Nightly 30s-5min blackout**: socket stays connected, no data flows, order
  placement fails silently. Any held position needs an exchange-side
  bracket/stop for that window.
- **`CLOSE_WAIT` socket rot**: a dropped connection that isn't cleanly closed
  leaves the process looking alive (PID present) while doing nothing — needs
  an external watchdog on `ss -tnp`, not just a process-alive check.
- **No open/close semantics** — only `BUY`/`SELL`. A SELL larger than the
  current long silently becomes a short; the risk layer must track net
  position itself.
- **CBOE's "390 rule"**: average >1 option order/minute across a month and
  the account gets reclassified "professional" (worse fees, worse execution
  priority) — a rate cap belongs in the guard layer before this ever goes live.
- Pacing: max 60 historical-data requests/10min (Error 162 if exceeded), 100
  concurrent streaming quotes by default, Docker/WSL2 needs Gateway's
  loopback-only restriction lifted or a `socat` relay.

### MCP servers worth connecting
Named, non-redundant against what's already connected (webull, momentum,
tickertrace, traderdaddy, tradingview, context7):
- **SEC EDGAR** / **ShareSeer** — 10-K/10-Q text mining and Form 4 insider
  data; cross-references against TickerTrace's institutional flow.
- **Polymarket** — prediction-market pricing as a real-time probability
  matrix for macro events (Fed decisions, FDA approvals).
- **LunarCrush** — social sentiment/hype metrics, if a sentiment-analysis
  pipeline ever gets built (aspect-based, multi-entity scoring — not
  document-level — is the credible approach per the sources reviewed).
- **QuantConnect** — cloud-scale backtesting engine, candidate
  alternative/complement to `vectorbt` for Module 4.
- Lower priority for a solo system: Alpaca (redundant broker), FIXParser
  (institutional order routing), dune-analytics-mcp/Twelve Data (only
  relevant if this goes multi-asset).

### Not yet researched
A few more of Michael's NotebookLM notebooks looked relevant but weren't
queried: *TraderDaddy Pro Docs & How-Tos* (might surface unused endpoints —
worth checking since TraderDaddy is already a core dependency), *Modern
API-First Brokerage and Algorithmic Trading Systems*, *The Only Trading
Library You'll Ever Need*, *The End of the Hedge: Global Macro and Regime
Shifts*, *Global Financial Markets: Volatility, Derivatives, and Risk*.

### 🗣️ LLM layer + voice — notes only, nothing built or installed yet

This is the response to "Vesper has no LLM reasoning layer" above. Written up
so the shape is decided before anyone (Flash included) starts wiring it, not
discovered by trial and error.

**Model: OpenRouter, `deepseek/deepseek-v4-flash`.** Already decided in
`docs/OPENROUTER_PRICING_GUIDE.md` — $0.03/$0.10 per 1M tokens in/out, 1.31M
context, explicitly recommended there for "continuous background screening,
high-frequency market polling, bulk technical parsing" — i.e. exactly the
always-on agent loop this gap describes. A 24/7 scanning loop on GPT-4o was
priced there at ~$50-100/month; on this model, under $1/month. No reason to
look elsewhere for the default model; `deepseek/deepseek-v4-pro` is the
escalation path in the same table for anything needing deeper multi-step
reasoning than Flash handles well.

**Where it plugs in**: `playbooks_node` is the natural first target — it's
already drafting proposals from deterministic signals; an LLM call there
would go from "encode Michael's exact rules in Python" (the momentum_squeeze
mismatch/ADX-IV-router/etc. items above) toward "reason over the same signals
the way the rules describe," with the deterministic version as a fallback/
sanity-check rather than a full replacement. Whether that's the right
target vs. a separate `analyst_node`-style reasoning layer is a real design
decision, not decided here.

**Voice + channel architecture — reuse Module 2, don't adopt an external
framework.** Explored `princezuda/safestclaw` and `moltis-org/moltis` (from
the `machinae/awesome-claws` list) as potential ready-made assistants to
adopt wholesale. Neither is the right call: both would stand up a *second*
system that then needs its own bridge into the same MCP servers
(webull/traderdaddy/tickertrace/momentum) Vesper already talks to — a
parallel stack, not a shortcut, since `vesper/bot/` already has the channel
infrastructure (`ChannelManager`, Discord/Telegram/webhook adapters,
`vesper/bot/inbound.py`'s `approval_registry`). What's worth stealing from
each as a pattern, not a dependency:
- **SafestClaw's voice pipeline**: local Whisper STT + Piper TTS, fully
  offline, no cloud round-trip or per-request voice API cost. The reference
  shape for a voice-message-in/voice-note-out flow bolted onto the existing
  Discord adapter: transcribe inbound audio locally, route the text through
  the same chat/decision surface as a typed message, synthesize the reply
  locally instead of returning text-only.
- **Moltis's "MCP client + multi-provider + multi-channel" shape**: confirms
  the overall architecture (one process, MCP client to existing tool servers,
  swappable model provider, several chat channels) is a proven, common
  pattern — validates building this directly into `vesper/bot/` rather than
  it being a novel design.

Neither project confirmed explicit OpenRouter support by name (both use
generic "custom endpoint" multi-provider config) — since OpenRouter is
OpenAI-API-compatible, this is very likely a non-issue, but wasn't verified
directly against either project's provider config and shouldn't be assumed
working without a quick check if either is ever actually touched.

**Not decided yet, deliberately**: whether voice-in comes from a Discord
voice message, a Telegram voice note, or something else; whether TTS output
posts as a voice-note reply or stays text-only with voice reserved for
alerts; and whether this belongs in `vesper/bot/` as extensions to the
existing adapters or as a new sibling module. Flagging the shape, not
committing to the implementation.
