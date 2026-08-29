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

**Start here, in this order** — cheapest/lowest-risk first, the one thing
that actually needs careful design saved for last so it doesn't get rushed:

1. **Housekeeping (done)**: `--persona traderlady` wired through `TradingState`,
   `vesper/runner.py`, and `vesper.py`; `vesper/whop.py` wired to `runner.py`,
   `vesper.py` (`--license-key`), and tested; `vesper/morning.py` fallback SPY/QQQ
   levels explicitly labeled as `STALE / UNAVAILABLE` with status indicators.
2. **Paper ledger close path (done)**: Wired `monitor.py`'s dry-run exit fills
   directly to `close_paper_position()`, updating position status to `CLOSED` and
   calculating realized PnL and account cash upon take-profit/stop-loss triggers.
3. **`PublicBrokerClient` buying-power lookup (done)**: Added `get_buying_power()`
   to `PublicBrokerClient` reading portfolio cash/purchasing power and plumbed
   into `ExecutionGuard`'s `preview()` handshake in `vesper/nodes/executor.py`.
4. **Bounce 2.0 precision (done)**: Added Slow Stochastic(8,3)≤40 pullback filter,
   `RSI(2)` dip trigger, and true Keltner Channel math (`ta.kc` length 14, 2x ATR)
   in `mcp_server/technicals.py`, `vesper/state.py`, `vesper/nodes/analyst.py`,
   and `vesper/nodes/playbooks.py`.
5. **Module 2's inbound ingestion layer + auth (done)**: Built `create_inbound_app()`
   aiohttp server with Telegram secret token verification (`X-Telegram-Bot-Api-Secret-Token`),
   Discord Ed25519 signature verification (`X-Signature-Ed25519` / `X-Signature-Timestamp`),
   and REST Bearer auth (`Authorization: Bearer <TOKEN>`) with LangGraph thread resume.
6. **LLM Reasoning & Risk Red-Teaming (done)**: Wired OpenRouter `audit_proposal_risk()`
   into `risk_gate_node` for qualitative trade evaluation and position size adjustment.

- **LLM reasoning: half landed.** `vesper/llm.py` (OpenRouter,
  `deepseek/deepseek-v4-flash` default) is wired into `playbooks_node` via
  `generate_candidate_thesis()` — but it only appends a narrative string to
  `audit_notes` *after* the proposal (quantity/price/side) is already fully
  constructed, so it cannot influence sizing or execution. Verified this
  directly by reading the call site. `audit_proposal_risk()` (an LLM
  red-team check on a proposal) exists in the same file but is **never
  called from anywhere** — dead code, same pattern as `vesper/whop.py`
  below. `analyst_node`/`regime_node`/`scanner_node` remain pure
  deterministic Python. See "LLM layer + voice" below.
- **Callback receiver: registry + resume logic exists, but nothing feeds it
  real events yet.** `vesper/bot/inbound.py`'s `ApprovalRegistry` correctly
  uses LangGraph's `Command(resume=decision)` (the right mechanism — verified
  it doesn't call `executor_node`/a broker directly), and `human_gate_node`
  polls `approval_registry.get_decision(p.id)` as a fallback path. **But
  `handle_callback_payload()` and `set_graph_app()` are never called from
  anywhere in the codebase** — grepped to confirm. There's no HTTP server,
  webhook route, or Telegram/Discord polling loop that would ever hand this
  registry a real inbound tap. Tapping "Approve" on a sent card currently
  does nothing. **When the ingestion layer gets built, it needs auth**:
  `handle_callback_payload` currently trusts any payload shape it's handed —
  no Telegram secret-token check, no Discord Ed25519 signature verification
  (Discord's own Interactions API requires this to even register an
  endpoint, which will force the issue there, but a generic REST webhook
  path has no such forcing function and would let anyone who can reach the
  endpoint approve a live trade or POST `{"command":"halt"}`).
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
- ~~`requirements.txt` predates the Vesper migration~~ — fixed, now has
  `langgraph`, `pydantic>=2.0`, `python-dotenv>=1.0`, `typing_extensions>=4.0`,
  `chromadb>=0.5`.
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

### 🟡 Module 2 — Channel-Agnostic Alert Bot (outbound done, inbound half-built)
`ApprovalChannel` interface with Telegram/Discord/webhook adapters, broadcast
from `human_gate_node`/`executor_node` — this half is done. `vesper/bot/inbound.py`
has the resolve/resume *logic* (`ApprovalRegistry`, correct `Command(resume=...)`
usage) but nothing calls it yet — no HTTP route, no Telegram/Discord listener.
See Known Gaps above for the auth requirement once that ingestion layer gets built.

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

Spot-checked `_compute_stats` (highest-risk function — a wrong Sharpe/
drawdown formula would silently mislead every result): Sharpe/Sortino/CAGR/
max-drawdown/profit-factor are textbook-correct with proper divide-by-zero
guards. Did not review the full 1292 lines.

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

Verified independently: dedup is real (UUID suffix + dedup-on-load/save by
id and semantic tuple), `get_playbook_performance()` reads the exact schema
`log_conviction()` writes (checked both sides), `reflection_node` reads
direction off `prop.side` not message-sniffing, all four origins get logged
with guards against double-counting a proposal appearing in multiple state
lists, and `trade_memory` reuses `knowledge.py`'s existing Chroma client
rather than duplicating it.

### ✅ Module 6 — Leveraged ETF Proxy & Skills Library (done)
`playbooks_node` queries `data/leveraged_etfs.db` for risk-scaled 2x
alternates (fetches a **real quote** for the proxy ticker via `md.Market` —
an earlier version reused the underlying's price, which was wrong and fed a
fabricated number into the notional-cap guard; fixed). `scanner_node`
integrates `mcp_server/vcp_screener`/`screener` and TickerTrace flows.
`vesper/skills_engine.py` does path-traversal-validated autonomous skill
authoring. 5 tests in `tests/test_leveraged_and_skills.py`.

### 🟡 Module 7 — Paper Ledger (opens-only) & Remote Kill Switch (done)
- [x] `vesper halt` CLI + `vesper resume` + `/halt` bot command for instant
      remote freezing, integrated into `ExecutionGuard` and CLI telemetry.
      Reviewed: file-based, atomic write (tmp + `os.replace`), checked in
      `execution_guard._validate` before the trading-enabled check.
- [x] `record_paper_fill()` logs every `DRY_RUN_SIMULATED` fill from
      `executor.py`. Had a real bug (fixed): only debited cash on BUY/LONG,
      never credited it on SELL/SHORT — would have double-counted proceeds
      the moment a short position closed. Fixed, `tests/test_paper_ledger_and_halt.py`
      passes.
- [ ] **`close_paper_position()` and `mark_to_market()` are never called from
      any node.** `mark_to_market` is reachable manually via `vesper paper
      --mark`; `close_paper_position` is called from nowhere but its own
      test. Net effect: every paper fill opens a position that nothing ever
      closes — `vesper/monitor.py`'s exit-cascade dry-run fills don't call
      `close_paper_position`, so paper positions accumulate open forever
      instead of tracking real round-trip P&L. Needs `monitor.py`'s dry-run
      exit path wired to `close_paper_position`, matched by ticker/proposal
      to the open fill it's closing — `record_paper_fill` currently has no
      open-vs-close distinction, so a naive wire-up would open a *new*
      position instead of closing the existing one.

---

## 💡 Ideas Backlog (speculative — not committed, no phase number)

### ⭐ Do first: gaps against Michael's own documented strategy
Queried Michael's own NotebookLM strategy notes (heavily Simon Ree's *Tao of
Trading*) against the three live playbooks. Highest-signal finding of any
research pass done on this repo:

- **`momentum_squeeze` was coded backwards — now landed as "Bounce 2.0",
  partially matching Michael's rule.** Was a breakout filter
  (`ema_stack==BULLISH or rsi_14>50`); `playbooks_node` now implements a real
  pullback/mean-reversion entry (bullish EMA stack, `ADX≥18`, price within
  ±1.5 ATR of the 21 EMA as an "Action Zone" approximation, `RSI≤68` not-
  overbought filter, vol-targeted sizing). **Still doesn't match Michael's
  exact rules**: no Slow Stochastic(8,3)≤40 check, no `RSI(2)` dip-below-10-
  then-cross-back-above-10 entry trigger (uses a looser `RSI>45` momentum
  filter instead), no explicit Keltner-Channel-length-14/2x-multiplier
  calculation (uses a flat ATR band) or "close above the pullback's low
  candle" confirmation. Directionally correct now, not exact — worth a
  follow-up pass against the precise notebook rules if this needs to match
  what Michael actually trades rather than approximate it.
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

### 🗣️ LLM layer + voice

**Model/text half: landed.** `vesper/llm.py` + `deepseek/deepseek-v4-flash`
via OpenRouter, wired into `playbooks_node` for thesis narratives. Setup and
usage now live in `docs/OPENROUTER_PRICING_GUIDE.md` rather than here — this
section is what's still *not* built.

**Still open**: `audit_proposal_risk()` exists in `vesper/llm.py` but is
called from nowhere — an LLM red-team check on a proposal, currently dead
code. Whether it's worth wiring in (as an advisory signal alongside, never
instead of, `execution_guard`'s deterministic checks) or left unused is an
open call. Whether `playbooks_node`'s thesis-only integration is the right
ceiling, or whether an LLM should ever influence sizing/entry logic itself
(vs. today's narrative-only, zero-influence role) — the Bayesian-network
pattern in "AI agent architecture" below is the more rigorous version of
that question, and better thought through before an LLM gets any real
influence over a proposal's numbers, if that's ever wanted.

**Voice: not built yet, but the decision is made — standalone, own voice
stack, DeepSeek stays the brain.** (2026-08-28) Considered wiring Vesper's
voice interface into `nyx` (`mphinance/nyx` on host `coolify`, née a fork of
`six-ddc/disclaw`) instead of building Vesper's own — nyx already has a full,
running voice pipeline (Discord voice channel, wake-word, Whisper STT +
Kokoro TTS both via OpenRouter, hub routing into a Claude Agent SDK brain)
that would have been a small integration (one more key in its
`createMcpServers` factory). **Decision: keep Vesper standalone.** Reasons
that mattered most:
- **Blast radius mismatch.** nyx also runs the TD Pro agent launcher, Sleeper
  fantasy cards, and other business-facing automation — a live trading
  assistant's uptime/security would depend on a system that also runs
  fantasy football.
- **Shared, budgeted Claude runner.** nyx's agent-run poller caps concurrency
  and daily runs specifically because it's shared across every other nyx
  thread — a trading voice query would queue behind unrelated business-agent
  runs.
- **Widens the exact surface this whole roadmap has been careful about.**
  Putting Vesper's tools behind a general-purpose assistant that also
  handles business ops means a stray utterance in the wrong channel is
  adjacent to trading tools instead of isolated from them — same class of
  risk as the inbound-approval-auth gap already tracked above, just
  relocated rather than solved.
- Not merged to `main` on the nyx side either — building on it would stack
  two moving targets.

**What's still worth reusing from nyx as validated reference, not as a
dependency**: the exact model pair — STT `openai/whisper-large-v3-turbo`,
TTS `hexgrad/kokoro-82m`, both callable via OpenRouter, both already proven
in production for trading-adjacent speech — and the vocabulary-biasing
trick (`voice/config.ts`'s `STT_VOCAB`): feeding Whisper an initial-prompt
hint of tickers/options jargon (their example: without it, "Nyx" transcribed
as "Nix" — the same class of mistake CLAUDE.md's old sidecar notes already
warned about for NVDA→"in video"). Same fix, cheaper than a bigger model,
worth carrying into Vesper's own STT config verbatim.

Also still relevant from the earlier `princezuda/safestclaw` /
`moltis-org/moltis` research: both validate the same overall shape (one
process, MCP client to existing tool servers, swappable model provider,
multi-channel) that `vesper/bot/` should be built as an extension of, not
adopted wholesale as external frameworks — same reasoning as the nyx
decision above, one level down.

**Decided: both sides get logged as text, always.** Whatever channel voice
ends up on, the transcribed input *and* the spoken-reply text both get
written to a durable text record — not just spoken and gone. This isn't
optional/configurable: audio is ephemeral and unauditable, and a system that
can act on spoken trading commands needs the same auditability as everything
else here (the `audit_trail` entries every node already writes, the
conviction journal, `execution_guard`'s ticket digests). Natural home is
alongside those — e.g. an entry in the existing audit trail or a dedicated
voice-transcript log, not a new parallel logging scheme. Whatever posts the
Discord/Telegram message already *is* that text log if voice-in posts its
transcript as a visible message before acting on it (nyx's own
`onVoiceUtterance` does exactly this — posts `🎙️ <@user>: {text}` to the
thread before submitting it) — worth copying that pattern rather than
inventing a separate one.

**Not decided yet, deliberately**: whether voice-in comes from a Discord
voice message, a Telegram voice note, or something else; whether TTS output
posts as a voice-note reply or stays text-only with voice reserved for
alerts; and whether this belongs in `vesper/bot/` as extensions to the
existing adapters or as a new sibling module. Flagging the shape, not
committing to the implementation.
