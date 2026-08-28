# Ideas Backlog

Speculative, not-yet-scoped ideas — separate from `ROADMAP.md`'s committed
Modules 0-7. Nothing here is planned; this is where "worth doing eventually"
lives until it earns a real Module. Sourced from three passes:

1. A research fork (2026-08-28) checking current AI-agent guardrail/memory
   techniques and retail-quant trading innovations via web search.
2. The "If Tony Stark opened this repo" section of
   [`docs/MODULE_REVIEW_2026-08-28.md`](MODULE_REVIEW_2026-08-28.md) —
   summarized here with a pointer, not repeated.
3. Two of Michael's existing NotebookLM notebooks — *"Python Automated
   Options Wheel Strategy and TradingView Screening"* (42 sources) and *"AI
   Trading and Sentiment Analysis Guide 2026"* (37 sources) — queried
   2026-08-28.

## Already summarized elsewhere (don't re-read here)

`docs/MODULE_REVIEW_2026-08-28.md`'s JARVIS section: one always-on graph
instead of separate CLI verbs, push-driven position events via gRPC instead
of 15s polling, regime-adaptive sizing instead of static thresholds, loud
failure/paging instead of silent log lines, a voice-triggered kill switch.

## AI agent architecture

- **LLM-as-model-builder, not decision-maker (the big one).** An arXiv paper
  on the options wheel strategy (*"A Hybrid Architecture for Options Wheel
  Strategy Decisions: LLM-Generated Bayesian Networks for Transparent
  Trading"*) has the LLM's job be building a **Bayesian network DAG** for
  each decision — nodes like Market Regime, Volatility Level, Technical
  Position, Assignment Probability; edges as causal relationships — while a
  deterministic engine (`pgmpy`) populates conditional probability tables
  from historical data and does the actual inference. The LLM never computes
  a probability itself; it only proposes structure. Their system: 15.3%
  annualized return, Sharpe 1.08 vs. 0.62 benchmark, -8.2% max drawdown vs.
  -60%, and — the part that matters most for us — **every trade decision
  carries ~27 recorded, human-auditable decision factors**. This is a much
  more rigorous version of "explain the proposal card" than free-text thesis
  strings: `playbooks_node` could emit a small DAG (regime → strike
  selection → assignment risk) instead of prose, with the probabilities
  coming from Module 5's outcome-memory data once it exists, not the LLM's
  own guess. Natural fit once Module 5's structured journal has enough
  resolved entries to populate real conditional probabilities instead of
  priors.
- **IntellAgent-style automated evaluation for the human-approval/chat layer.**
  A framework for testing conversational agents: build a **policy graph**
  (nodes = rules like "never exceed notional cap," edges = how often two
  policies co-occur in one conversation), do a weighted random walk to
  generate test scenarios at a target difficulty, run an automated **user
  agent** that converses with the system under test against a symbolic mock
  database, then have a separate **Dialogue Critique agent** audit the
  transcript and report exactly which policies were tested and which broke.
  Directly applicable to `human_gate_node` and any future chat/bot layer —
  turns "did the approval flow behave correctly" from manual QA into a
  generated, repeatable test suite. Bigger lift than most things here; worth
  it once there's an approval/chat surface complex enough to need it (i.e.
  once Module 2's callback receiver exists).
- **Risk-scaled guardrail intensity** *(from the research fork)*: scale
  `execution_guard.py`'s caps/TTL to context instead of one static rule —
  tighter when `regime.health_score` is low, looser for playbooks with a
  proven track record via Module 5.
- **A cheap consistency check before `human_gate_node`** *(from the research
  fork)*: verify a proposal's thesis text actually matches
  `state["technicals"]`/`state["regime"]` before it reaches a human — the
  Bayesian-network idea above is the fuller version of this same problem.
- **Graph-structured memory (Mem0/Cognee-style) — v2, not now.** The rung
  past vector-only memory once `trade_memory` needs relationship questions
  ("what's correlated with what, over time") that similarity search can't
  answer. Module 5's plan is right for current scale.

## Trading-specific

- **Vol-targeting position sizing — do this one first.** `vesper/risk.py`
  uses flat constants (`DEFAULT_MAX_RISK_PCT = 0.02`) regardless of current
  volatility. Standard formula: size ∝ `target_vol / realized_vol`.
  `TechnicalAudit` already computes ATR — this is a formula change in
  `RiskEnforcer.calculate_equity_size`, not new infrastructure. Best
  effort-to-impact ratio of anything in this doc.
- **Holly (Trade Ideas AI)'s nightly re-optimization pattern.** Every night,
  re-backtest ~60 strategy variants against the day's data; only activate,
  for the next session, the ones clearing a bar (their cutoff: >60% backtested
  win rate, 2:1 reward:risk). This is Module 4 (backtester) and Module 5
  (outcome memory) combined into a nightly job that actually gates which
  playbooks `scanner_node` runs tomorrow — not just scores them after the
  fact.
- **Risk-segmented playbook "personas."** Holly ships distinct bot profiles
  (conservative/moderate/aggressive) rather than one blended strategy. Maps
  onto Vesper's existing `--playbook` selector — could become a genuine
  risk-tier selector (`VESPER_RISK_PROFILE=conservative`) that scales
  `execution_guard.py` caps and `RiskEnforcer` thresholds together, instead
  of the playbook flag and the risk constants being independent today.
- **Continuous streaming over polling, with product precedent.** Holly
  streams alerts in real time rather than checking on an interval —
  reinforces the gRPC push idea already in the JARVIS list, now with a
  concrete "this is how a real product does it" reference.
- **Hedge-vs-directional options flow classification** *(from the research
  fork)*: score whether a large print is a directional bet or a
  dealer/institutional hedge before it becomes a `Candidate` — rule-based
  start (trade size vs. OI, IV skew, proximity to gamma flip) on top of data
  TraderDaddy already provides.
- **Aspect-based, multi-entity sentiment analysis — only if/when news
  ingestion gets built.** Not built today, so this is speculative: document-
  level sentiment scoring is unreliable (fails on negation/sarcasm); a
  credible approach ties sentiment to a specific aspect ("earnings surprise,"
  not just "positive") and, for a multi-ticker article, scores each mentioned
  ticker independently rather than one score for the whole text. Relevant
  the day Module 1's "institutional whale flow" briefing grows a news-reading
  component.
- **Tooling worth knowing about, not necessarily adopting wholesale:**
  `vectorbt` (vectorized backtesting, parallel hyperparameter sweeps —
  directly applicable to Module 4), `quantstats` (Sharpe/Sortino/drawdown tearsheets,
  including a Monte Carlo "probability of hitting -20% drawdown before +50%
  target" simulation that could enrich the human-approval card), `py_vollib`
  (fast, precise Black-Scholes IV/Greeks if options math ever needs to move
  in-process instead of relying on TraderDaddy), `OpenBB` (unified free data
  layer, mostly redundant with what TraderDaddy/TickerTrace already provide
  here but worth knowing about as a fallback).

## ⭐ Michael's own documented strategy vs. what's actually coded

Queried *The Investing and Trading Strategies of MPHinance* (Michael's own
notes, heavily influenced by Simon Ree's *The Tao of Trading*) against the
three playbooks that exist today (`0dte_flow`, `momentum_squeeze`,
`institutional_convergence`). This is the highest-signal finding of the
whole research pass: **`momentum_squeeze` doesn't run the strategy it's named
after**, and four entire strategies from Michael's own methodology have no
code at all.

### `momentum_squeeze` is coded as breakout entry; Michael's actual rule is the opposite

`vesper/nodes/playbooks.py` currently drafts a proposal on
`tech.ema_stack == "BULLISH" or tech.rsi_14 > 50` — that's a breakout/momentum
continuation filter. Michael's actual rule (per Simon Ree's methodology)
explicitly **avoids breakouts as false-breakout traps** and trades **mean-
reversion pullbacks** instead ("Bounce 2.0"):
- Trend filter: EMA stack bullish (`8 > 21 > 34 > 55 > 89`) **and** `ADX(13) ≥ 20`
- Pullback trigger: price back inside the Keltner Channel "Action Zone"
  (±1 ATR of the 21 EMA, Keltner length 14, 2x ATR multiplier)
- Momentum exhaustion: Slow Stochastic(8,3) ≤ 40
- Entry: `RSI(2)` dips below 10 then crosses back above it, confirmed by a
  daily close above the pullback's low candle
- Exit: 50% of size at +2 ATR, remaining 25% at +3 ATR

This is a precise, backtestable rule set, not a vague preference — worth
either rewriting `momentum_squeeze` to match it or renaming the current
breakout logic to something else so the two don't get confused.

### Four strategies with zero code today

1. **ADX/IV option-style router.** A decision matrix: `ADX(13) < 20` + `IV <
   70%` → Wheel (sell CSPs); `ADX ≥ 20` + `IV < 70%` → LEAPS; `ADX ≥ 20` + `IV
   ≥ 70%` → Synthetic long (buy call + sell put, same strike, ~4x leveraged
   stock-equivalent without margin interest); `ADX < 20` + `IV < 70%` → buy
   shares outright ("Training Wheels"). A clean `playbooks_node` addition —
   the inputs (ADX, IV) are already computable from existing data.
2. **Premium-recycling "free share" engine.** Sweep 100% of realized options-
   selling/day-trading profit into buying shares of the underlying or a
   stabilizer (`SGOV`/`NTSX`) until a full 100-share block accumulates,
   becoming a permanent zero-cost-basis holding. This is a portfolio-state
   machine, not a signal — would live closer to Module 5's outcome tracking
   than to `playbooks_node`.
3. **"Thega" delta-neutral volatility harvesting.** For high-IV binary events
   (earnings, biotech catalysts): buy 100 shares + sell 1 ATM covered call +
   sell 3 ATM cash-secured puts, netting `Δ_call − 3×Δ_put ≈ 0`. Harvests
   theta/vega crush direction-indifferently. Needs live per-contract delta
   from Webull's options chain — not currently pulled into any node.
4. **YieldMax `$ULTY` collar-following.** Covered-call ETFs like `$ULTY` are
   structurally forced to buy protective puts against their concentrated
   holdings; parse their daily holdings file, find the exact strikes they
   bought puts at, and sell cash-secured puts at those same strikes — if
   assigned, shares transfer in at a YieldMax-subsidized discount. Needs a
   daily holdings-file scraper; nothing else here reads ETF holdings data
   today.

### Rules that should tighten `0dte_flow`

- Only run single-stock weeklies where IV > 70% (ideally >100%) — targets
  2-4% weekly ROC.
- Sell puts at 0.30 delta, or at major put-wall open interest strikes.
- Reject wide-spread chains (e.g. $5 strike intervals on a $6 stock).
- Earnings week: sell ATM CSPs for the vega, buy-to-close the day after IV
  collapses.

### Portfolio-level risk rules not in `vesper/risk.py` today

- **15% trailing NLV stop**, peak-to-trough on total portfolio value — closes
  everything and pauses all trading 24h if hit. This is a portfolio-wide kill
  condition on top of `execution_guard.py`'s per-order caps, not a
  replacement for them.
- Swing option stops keyed to the **underlying's** price level (close below
  200 SMA / 34 EMA / lower Keltner band), not a fixed percentage on the
  option contract itself — Michael's notes explicitly call fixed-% option
  stops whipsaw-prone.
- Capital allocation buckets: 15% sector/thematic swings, 15% equity options
  (max one open long option position at a time), 20% wheel-stock allocation.
- Tax rule: route 25% of high-yield distributions (`$MSTY`/`$ULTY`) to
  `$SGOV` automatically.

None of this is scoped into a Module yet — it's a strong case for a
dedicated "Michael's Edge" module once Modules 0-5 are stable, since unlike
the rest of this backlog, it's not speculative: it's strategy Michael already
trades manually and trusts.

## IBKR broker integration — concrete gotchas for Phase 4

Queried *Interactive Brokers API Automation and 2FA Integration Guide*.
Genuinely load-bearing if/when `vesper/brokers/ibkr_broker.py` gets built
(mirroring `public_broker.py`'s shape per `ROADMAP.md` Phase 4):

- **One session per account, globally.** TWS, Gateway, Client Portal Web API,
  and mobile all fight over a single login slot — checking a position on your
  phone kicks the running bot offline. Fix: create a **separate IBKR user**
  for the bot (no withdrawal rights, IP-restricted), never share credentials
  with the account you check manually.
- **Weekly forced reset.** IBKR does a mandatory crypto/session reset every
  Saturday night. Paper accounts without 2FA can auto-restart cleanly
  (`ColdRestartTime` in Gateway config); **live accounts need a human to
  acknowledge 2FA on the IBKey app at least once a week** — this can't be
  fully unattended.
- **Daily soft-restart, not logoff.** Configure Gateway/IBC for auto-restart
  (reuses cached credentials) rather than auto-logoff, or 2FA is needed
  daily instead of weekly.
- **A nightly 30s-5min blackout** where the socket stays connected but no
  data flows and orders fail silently — any position held through it needs
  an exchange-side bracket/stop, since the API is blind during the window.
- **`CLOSE_WAIT` socket rot**: a dropped connection that isn't cleanly closed
  leaves the process looking alive (PID present, systemd "running") while
  actually doing nothing. Needs an external watchdog checking `ss -tnp`, not
  just a process-alive check.
- **No open/close semantics.** IBKR's API only knows `BUY`/`SELL`, not "buy
  to open" — a SELL larger than your current long silently becomes a short.
  The risk layer must track net position itself, same as
  `execution_guard.py` already should for any broker.
- **CBOE's "390 rule"**: average more than 1 option order/minute across a
  month and the account gets reclassified "professional" — materially higher
  data fees, worse execution priority. A rate cap belongs in the guard layer
  if IBKR options ever go live.
- Pacing: max 60 historical-data requests per 10 minutes (Error 162 if
  exceeded); 100 concurrent streaming quotes by default; Docker/WSL2 needs
  Gateway's loopback-only restriction lifted or a `socat` relay.

## Data/infrastructure APIs — mostly institutional-scale, one exception

Queried *Financial Markets Data APIs and Trading Infrastructure Reference*.
Most of what's covered (ORATS SVI vol-surface fitting, ThetaData's local
terminal architecture, DTN IQFeed raw TCP ticks, Cboe DataShop TBT via
SFTP/Snowflake, SpiderRock MLink) is genuinely institutional-grade — overkill
for this system per the same "not hedge-fund scale" filter applied
throughout this backlog. **One exception worth flagging**: FlashAlpha/ORATS
compute **Vanna (VEX) and Charm (CHEX) exposure** alongside the Delta/Gamma
exposure this system already tracks via TraderDaddy's dealer gamma (GEX).
Same conceptual family as GEX, just second/third-order — worth checking
whether TraderDaddy already exposes these before building anything new.

## MCP servers worth connecting

Queried *Model Context Protocol Server Directory* against what's already
connected (webull, momentum, tickertrace, traderdaddy, tradingview,
context7). Concrete, non-redundant, named servers:

- **SEC EDGAR** (Stefano Amorelli) — programmatic 10-K/10-Q text mining,
  executive change filings. Could cross-reference against `tickertrace`'s
  institutional flow for "big buyer + risk-factor change" convergence.
- **ShareSeer** — real-time SEC filings plus **Form 4 insider trading data**.
  Same cross-reference value as EDGAR, more real-time.
- **Polymarket** — prediction-market pricing as a real-time probability
  matrix for macro events (Fed decisions, FDA approvals) — a genuinely
  different signal type from anything currently ingested.
- **LunarCrush** — social sentiment/hype metrics, token-efficient for LLM
  consumption. Relevant if the sentiment-analysis idea above ever gets built.
- **QuantConnect** — cloud-scale backtesting on an institutional engine;
  candidate alternative/complement to `vectorbt` for Module 4 specifically.
- Lower priority for a personal system: **Alpaca** (redundant broker, already
  have Webull+Public.com), **FIXParser** (institutional order routing,
  not relevant at retail scale), **dune-analytics-mcp**/**Twelve Data**
  (on-chain and macro feeds — only relevant if this ever goes multi-asset).

## Notebooks not queried — deliberately out of scope for this pass

- *Modern API-First Brokerage and Algorithmic Trading Systems*
- *TraderDaddy Pro Docs & How-Tos* (worth a look someday since TraderDaddy is
  already a core dependency — might surface unused endpoints)
- *The Only Trading Library You'll Ever Need*
- *The End of the Hedge: Global Macro and Regime Shifts*
- *Global Financial Markets: Volatility, Derivatives, and Risk*
