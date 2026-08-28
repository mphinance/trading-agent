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

## Notebooks not yet queried — flagged, not pulled

Michael's NotebookLM account has ~95 notebooks; most are ticker-specific
research or unrelated to this repo. These looked plausibly relevant to
Vesper specifically and weren't queried in this pass — worth a follow-up if
wanted:

- *Modern API-First Brokerage and Algorithmic Trading Systems*
- *Financial Markets Data APIs and Trading Infrastructure Reference*
- *TraderDaddy Pro Docs & How-Tos* (TraderDaddy is already a core dependency
  here — this one specifically might surface tools/endpoints not yet wired in)
- *Interactive Brokers API Automation and 2FA Integration Guide* (relevant to
  Phase 4's planned IBKR broker integration)
- *The Only Trading Library You'll Ever Need*
- *Model Context Protocol Server Directory* (could surface more MCP servers
  worth connecting)
- *The End of the Hedge: Global Macro and Regime Shifts*
- *Global Financial Markets: Volatility, Derivatives, and Risk*
- *The Investing and Trading Strategies of MPHinance* (personal strategy
  notes — could ground playbook design in Michael's own stated edge rather
  than generic patterns)
