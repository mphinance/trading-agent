# The `trading-agent` MCP server — what it is, and what the 80 tools do

*Written for someone who has seen the tool list and wants to know what sits
behind it. Current as of 2026-09-13. No repo access assumed.*

---

## The one-paragraph version

`trading-agent` is an MCP server that fronts a personal, single-operator
trading system called **Vesper**. Vesper is a LangGraph agent that scans the
market, analyses candidates, drafts an order, runs a deterministic risk gate,
asks a human to approve it over Telegram or Discord, executes, and then
monitors the open position for an exit. The MCP server is the *conversational*
way into that system — you talk to Claude, Claude calls the server, and the
server answers questions about the market and about the account. As of
2026-09-04 it can also originate an order. It cannot approve one.

Reachable at `https://agent.mphinance.com/mcp`. **80 tools.**

---

## What the 80 tools break down into

| group | count | what it reads | needs a credential? |
|---|---|---|---|
| Momentum / quant | 47 | yfinance, TradingView, SEC EDGAR, TraderDaddy Pro, local computation | 12 of them need a TraderDaddy Pro key; the other 35 need nothing |
| TickerTrace `etf_*` | 17 | institutional ETF holdings, conviction signals, cross-fund divergence | no |
| Vesper state (read) | 13 | the live account and the agent's own state | owner-only |
| Order path | 3 | the broker | owner-only, **and** a separate OAuth scope |

The first 64 are general-purpose market tooling — they'd work for anyone. The
last 16 only mean anything if you are the account holder.

### Momentum / quant — 47 tools

**Screening (6)** — `run_stock_screen` (TradingView scanner, 22 presets),
`run_custom_screen`, `screen_vcp` (Volatility Contraction Pattern),
`screen_canslim`, `screen_pead` (post-earnings drift), `sweep_setups`.

**Technicals & charts (6)** — `analyze_technicals` (24 indicators: EMA stack,
RSI, MACD, ADX, ATR, Bollinger), `get_tv_analysis` (TradingView's 26-indicator
consensus), `get_momentum_pulse`, `get_historical_data`, `generate_chart`,
`generate_alpha_card`.

**Options analytics (5)** — `analyze_options_setup` runs a VoPR™ engine:
composite realized vol from four estimators, variance-risk-premium ratio,
Delta/Theta, and an A–F grade. `find_best_to_sell` (7–45 DTE) and
`find_best_to_buy` (21–60 DTE) auto-search the chain.
`model_price_distribution` and `calculate_position_size` (fixed-fractional,
ATR, or Kelly) do the sizing maths.

**Market state & regime (10)** — `detect_market_top`, `detect_ftd`
(follow-through days), `detect_macro_regime`, `detect_bubble_risk`,
`detect_themes`, `analyze_breadth`, `analyze_uptrend_participation`,
`get_market_environment`, `get_exposure_recommendation`, `analyze_recent_gap`.

**Flow & positioning (8, TraderDaddy Pro)** — `get_unusual_activity`,
`get_sector_flow`, `get_gex_overview` (gamma exposure; the GEX flip level is a
regime boundary, not a target — see the caveat below), `get_put_call_ratios`,
`get_market_stats`, `get_market_pulse`, `get_signals`, `get_earnings_flow`.

**Fundamentals & filings (5)** — `get_fundamentals`, plus four straight from
SEC EDGAR: `get_sec_filings`, `get_sec_financials` (multi-period XBRL including
the accrual gap), `get_shares_outstanding` (the cover-page count, not a vendor
estimate), `get_stakes_held` (AS-FILER 13D/13G — stakes *this* company holds in
others, which is the direction nobody indexes).

**News, calendars, misc (7)** — `fetch_ticker_news`, `extract_article_text`,
`get_earnings_calendar`, `get_politician_trades`, `get_alpha_signals`,
`analyze_pair` (statistical arbitrage), `analyze_scenario`.

### TickerTrace `etf_*` — 17 tools

Institutional positioning derived from daily ETF holdings across 71 tracked
funds and ~2,270 underlyings. `etf_briefing` is the pre-market summary;
`etf_signals` gives conviction-scored buys and sells; `etf_layering_patterns`
finds the case where three or more independent stock-pickers opened the *same*
new position inside a window; `etf_divergences` finds the opposite — funds
trading the same name in opposite directions. Also per-fund and per-ticker
detail (`etf_fund_detail`, `etf_stock_activity`), option-income fund structure
(`etf_income_overview`, `etf_income_fund_detail`), newly optionable stocks from
the CBOE daily diff (`etf_options_listings`), and a historical backtest of the
signals themselves (`etf_signal_performance`).

### Vesper state — 13 read-only tools

This is the half that only exists because there's a live account behind it.

| tool | answers |
|---|---|
| `get_account_state` | live equity, buying power, open positions |
| `get_position_monitor_status` | what the exit cascade **would** do to each open position right now |
| `get_halt_status` | is the emergency freeze engaged, and why |
| `get_drawdown_status` | tracked peak NLV vs. the circuit-breaker threshold |
| `get_paper_positions` / `get_paper_summary` | the simulated book: NLV, realized/unrealized P&L, win rate |
| `list_alerts` | armed / pending / triggered price alerts, each dynamic level re-resolved |
| `list_pending_proposals` / `get_proposal` | orders waiting on a human's tap, and how one was decided |
| `get_audit_trail` / `verify_audit_chain` | the hash-chained ledger, and a walk that localizes any broken link |
| `get_playbook_calibration` | resolved win rate and calibration adjustment per playbook |
| `recall_similar_setups` | semantic recall — "what happened last time a setup like this showed up" |

`recall_similar_setups` is the interesting one. Every conviction the system
logs gets embedded into a vector store, and resolved outcomes are written back,
so the agent can be asked what its own history says about the setup in front of
it rather than what a backtest says about the market.

### Order path — 3 tools

`submit_manual_proposal_tool` → `place_from_ticket_tool` is the two-step path;
`place_order_tool` is the one-call version. All three sit behind a `trade`
OAuth scope that the long-lived static token does not carry.

---

## The safety model, since these tools touch a real brokerage account

Five things bound an order, and they are enforced in code, not in a prompt:

1. **`VESPER_TRADING`** is a kill switch that defaults **off**.
2. **A halt file** — an emergency freeze checked before anything else. A
   **circuit breaker** trips it automatically on a 15% trailing-peak drawdown.
3. **A portfolio-aware notional cap.** Per-order exposure is capped at
   `min(absolute ceiling, percentage × net liquidation value)`, and it **fails
   closed** — if the account value can't be read, the cap is zero and every
   opening order is refused. A flat dollar ceiling on a small account is
   decoration, so the percentage term is the one that usually binds. Closing
   orders skip the cap; they can't increase exposure.
4. **A daily order count limit.**
5. **The guard's own caps** — notional, quantity, optional symbol allowlist,
   optional buying-power fraction — inside the one module allowed to reach the
   broker.

Two design decisions worth calling out because they're the whole posture:

**Preview, then confirm, then place.** Staging an order runs the guards and
returns a ticket carrying a SHA-256 of the exact payload. Placing takes a
*ticket id*, never an order. So no single call can both construct and fire an
order, and what was approved is byte-for-byte what reaches the broker. Tickets
are single-use and expire in 120 seconds.

**A tool can originate an order. A tool can never approve one.** The functions
that resume a paused agent run or record an approval decision are unreachable
from every MCP module — mechanically, pinned by a test that parses the AST, not
by a convention someone could drift away from. Approval happens by tapping an
inline button in Telegram or Discord. The reasoning: a voice transcript is
ambiguous in exactly the wrong place ("approve" — *which* proposal?), while a
button is unambiguous, per-user authorised, and survives a restart.

There is also exactly **one module in the entire codebase that can move
money.** Everything else reads. Any adapter that grows its own order path is
treated as a new threat model rather than a small addition.

---

## Auth

Two credentials reach the same server, and they deliberately differ in what
they can do:

- **A static bearer token** carries `read` and `safe-write`. It does **not**
  carry `trade`. A long-lived secret sitting in a file on disk cannot place an
  order.
- **An OAuth 2.1 token** minted through an authorization gate that requires the
  operator secret, with a human present, can carry `trade`.

A consequence that looks like a bug and isn't: listing tools with the bearer
returns **77**, not 80, and calling an order tool with it answers
`Unknown tool`. MCP filters by scope rather than returning a 403.

---

## Beyond tools: 65 skill resources and 2 prompts

The server also exposes **65 skills** as MCP resources (`skill://...`) and two
prompts. The skills are playbooks — 0DTE flow rules, VCP and CANSLIM screening
methodology, breadth and regime frameworks, position sizing, dividend-growth
process, edge-research pipelines — so a client can load the *method* rather
than re-deriving it in the prompt. `skill://rules` is the operating contract
for the server itself.

The two prompts: `morning_brief` (pre-market) and `copilot_setup`, which is
designed to be invoked on a 30–60 second cadence for live setup monitoring.

---

## Two caveats that matter more than they look

**Dealer gamma is a map of positioning, not a forecast.** It marks where
hedging is concentrated, which is why price often *reacts* at those levels. It
never means price will travel there, and a wall above spot is not a reason to
be long. The common failure is a level getting repeated back as a target.

Relatedly, the gamma flip is a regime boundary, and two different tools
disagree about where it is — one reads it off the ladder, one simulates it.
When they straddle spot, the regime call is genuinely uncertain, and the tools
report that disagreement rather than silently picking one. The disagreement is
the signal.

**The LLM may narrate, reject, or shrink — never originate or increase.**
Strategies are deterministic Python. A language model appends narrative *after*
a proposal's numbers are already fixed, so it can't influence sizing or entry,
and the risk red-team runs only *after* the deterministic gate passed and may
only reject or halve the quantity. It can never approve what the deterministic
gate rejected, and it can never increase size. This is deliberate and has been
re-affirmed against more permissive alternatives: an LLM that can originate a
position is a different product with a different risk profile.

---

## What this is not

- **Not multi-tenant.** Single-operator personal tool. No authentication model
  beyond the operator, no user model, no accounts. That's a load-bearing
  assumption throughout, not a packaging gap.
- **Not a signal service.** The Vesper tools read one specific account.
- **Not fully exercised against a live broker.** The order path is tested end
  to end against a stub, and the request payload shapes are verified against
  the broker's non-committal preview endpoint — which proves the wiring, not
  the broker's acceptance of every order type. Multi-leg option combos beyond a
  single leg are refused at the executor on purpose, because the correct enum
  values are unverified and a guessed payload to a live order endpoint is not
  an acceptable way to find out.
