# Vesper — operational reference

This doc used to describe three surfaces: MCP tools as a thin HTTP client, a
42-route FastAPI app (`server.py`, `:8787`), and an `/api/stream` SSE feed.
Commit `de60d51` deleted that browser-dashboard architecture along with
`server.py`, `static/index.html`, `chat.py` and `orders.py` — see CLAUDE.md's
"History that will otherwise confuse you" for the full story. None of it is
coming back. This is a from-scratch rewrite describing what is actually here
today. See [README.md](../README.md) for setup/run instructions — this doc is
the detailed reference for the CLI, both MCP servers' tool inventories, and
the order path (now two of them, see below); it doesn't repeat what the
README already covers.

**Updated 2026-09-13 for Amendment A4 (2026-09-04).** A4 added a second,
owner-only MCP server (`trading_mcp/`) that can place a real order through
three scope-gated tools. Before A4 this document could correctly say "no MCP
tool can place an order" — that sentence is now false and every instance of
it below has been corrected. See "The order path" and "`trading_mcp/`
(owner-only surface)" for what changed and what didn't.

## What exists now

| Surface | What it is | Can place an order? |
| --- | --- | --- |
| **CLI** (`vesper.py`) | The operational surface. Scan/analyze/monitor/loop/listen/alerts/halt/status/paper/audit. | Only via the LangGraph pipeline + a human approval tap. |
| **MCP server** (`mcp_server/`, entrypoint `mcp_server/server.py`) | Read-only quant tooling — screeners, technicals, options analytics, macro/breadth, research, backtesting — exposed to MCP hosts over stdio (FastMCP; `MCP_TRANSPORT=sse` is supported by the code but nothing in this repo starts it that way). | **No.** No broker credentials, no `wb.py` import, no order-placement tool anywhere in the directory. This property is unchanged by A4 and is the whole reason `trading_mcp/` exists as a *separate* process instead of adding tools here. |
| **`trading_mcp/` server** (owner-only, entrypoint `trading_mcp/server.py`) | A **separate process** from `mcp_server/`. 80 tools = the same 64 momentum/TickerTrace tools re-registered from `mcp_server/registry.py`, plus 13 read-only Vesper-state tools, plus (as of Amendment A4, 2026-09-04) 3 order tools. Also exposes 65 `skill://` resources and 2 prompts (`copilot_setup`, `morning_brief`). Internet-reachable in production, behind auth — see its own section below. | **Yes, for a `trade`-scoped credential.** Three tools in `trading_mcp/order_tools.py` reach `vesper.execution_guard` directly, bounded by MCP-specific caps on top of the guard's own. A bearer-token credential cannot use them (see below). |
| **Telegram / Discord bots** (`vesper/bot/`) | Outbound-only approval channels — long-poll (Telegram) / gateway connection (Discord). Render proposal cards, resolve Approve/Reject taps, accept `/halt` and `/resume`. | Indirectly — a tap resolves a paused graph node, which is the only thing that can reach `executor_node`. |
| **HTTP** | Two different things share this row. `vesper/bot/inbound.py` defines `create_inbound_app()` (an aiohttp app with `/webhook/telegram`, `/webhook/discord`, `/webhook/approval`, `/health`, `/approvals`), and nothing in this repo calls it or runs `web.run_app()` on it — it's an alternative approval-delivery mechanism that was never wired up (CLAUDE.md rule 1). Separately, `trading_mcp/server.py` **does** serve HTTP in production — see the dedicated section below; it is not this dead code path. | Inbound app: N/A, not running. `trading_mcp`: see the row above. |

There is no SSE stream and no browser UI. Rate-limit budgets (2 req/2s
account reads, 600/min market data and order calls) live in `wb.py` and
`md.py` — see CLAUDE.md's gotchas section, not repeated here.

---

## CLI (`vesper.py`)

`./.venv/bin/python vesper.py [command] [ticker] [flags]`. Default command is
`scan` if none is given.

### Commands

| Command | What it does |
| --- | --- |
| `scan` (default) | Runs the full LangGraph pipeline once: regime → scanner → analyst → playbooks → risk_gate → human_gate → executor → reflection. |
| `analyze TICKER` | Same pipeline, scoped to one ticker (defaults to `SPY` if omitted). |
| `0dte` | Same pipeline with `--playbook 0dte_flow`, ticker forced to `SPY`. |
| `morning` | Runs `vesper.morning.generate_morning_plan()` — a standalone briefing, not the graph. |
| `monitor` | Runs `vesper.monitor.run_monitor_loop()` — one exit-cascade sweep (or continuous, see `--interval`/`--once`) over open positions. |
| `halt` | Emergency freeze via `vesper.halt.halt()`. Takes an optional `--reason`; omitted, it falls back to `halt()`'s own default message. |
| `resume` | Emergency-freeze release via `vesper.halt.resume()`. |
| `status` | Prints halt state, paper-ledger summary, health metrics written by a separately-running `vesper loop` (if any), and pending-approval ages. Report-only — never claims liveness of a process it isn't part of. |
| `paper` | Prints the paper-trading ledger (NLV, cash, realized/unrealized P&L, open positions). `--mark` runs `mark_to_market()` first. |
| `listen` | Runs the Telegram long-poll loop and Discord gateway concurrently, feeding `ApprovalRegistry` real Approve/Reject taps and `/halt`/`/resume` commands. Outbound-only, no port opened. Builds one graph instance and reuses it for resuming paused threads. |
| `loop` | Unattended daemon (`vesper/loop.py`): scheduled scans at fixed ET times + a continuous position monitor + the alert watcher thread, all in one process. `dry_run` by default; `--live` makes a scheduled scan draft proposals that still pause for a remote Telegram/Discord approval — run `listen` alongside it, or nothing gets approved. Skips a scheduled scan entirely while halted. No holiday calendar; weekends are skipped. |
| `alerts` | Arm/list/remove dealer-gamma alerts (see Alerts section below). Control-plane only — alerts are *evaluated* by the watcher thread inside a running `vesper loop`, not by this one-shot process. |
| `audit` | Verifies the hash-chained audit ledger's integrity via `vesper.audit_chain.verify_chain()`. `--verify` is accepted but currently a no-op placeholder flag (the command has only one mode today). |

### Flags

| Flag | Applies to | Meaning |
| --- | --- | --- |
| `ticker` (positional) | `analyze` | Target symbol. Defaults to `SPY` for `analyze`/`0dte` if omitted. |
| `--playbook` | scan-family commands | `all` (default), `momentum_squeeze`, `0dte_flow`, `institutional_convergence`, `collar_following`, `adx_iv_router`, `thega`, `recycle`, `tax_reserve`, `earnings_vega`. |
| `--persona` | scan-family, `loop` | `default` or `traderlady` — response voice. |
| `--live` | scan-family, `loop` | Enables live Webull execution *mode* — still gated by `VESPER_TRADING`, the deterministic risk gate, and a human approval tap. Does not skip anything. |
| `--non-interactive` | scan-family | Skips human confirmation prompts (used with `AUTO_DRY_RUN` semantics). |
| `--interval` | `monitor`, `loop` | Poll interval in seconds, default `15.0`. |
| `--once` | `monitor` | Run a single sweep and exit instead of looping. |
| `--license-key` | any | Validates a Whop commercial license key and exits — unrelated to trading. |
| `--arm SYMBOL LEVEL DIRECTION` | `alerts` | Arm one alert. `LEVEL` is a number or one of `flip`/`pin`/`wall_above`/`wall_below`. |
| `--disarm ID` | `alerts` | Remove an alert by id (id comes from `alerts`'s listing). |
| `--note` | `alerts` (with `--arm`) | Optional note attached to the armed alert. |
| `--verify` | `audit` | Accepted, currently a no-op. |
| `--reason` | `halt` | Optional reason recorded in the halt state; defaults to `halt()`'s own message when omitted. |
| `--mark` | `paper` | Mark open paper positions to market before printing the ledger. |

---

## The order path

**As of Amendment A4 (2026-09-04) there are two ways to reach Webull, not
one.** Before A4 this section could correctly say no MCP tool and no HTTP
route could place an order; that is no longer true, and CLAUDE.md rule 3 was
amended rather than broken to accommodate it. Both paths below terminate in
the same single module:

1. **The human-approved pipeline (unchanged).** The LangGraph pipeline drafts
   a proposal → the deterministic risk gate passes it → a human taps Approve
   on a Telegram or Discord card → `executor_node` calls
   `vesper/execution_guard.py`.
2. **The `trading_mcp/` order tools (new in A4).** A `trade`-scoped MCP
   credential calls `submit_manual_proposal_tool` → `place_from_ticket_tool`,
   or the one-call `place_order_tool`, in `trading_mcp/order_tools.py` — each
   reaches `vesper.execution_guard` directly, with **no human approval tap in
   this path at all**. What bounds it instead: `VESPER_TRADING`, the halt
   file, the circuit breaker, a portfolio-aware MCP-specific notional cap,
   a daily order-count limit, and the guard's own caps below. See
   "`trading_mcp/` (owner-only surface)" further down for the full mechanism,
   the scope model that gates it, and exactly what still can't be done this
   way (approving a *pending* proposal — `resume()` and
   `ApprovalRegistry.submit_decision()` remain unreachable from every MCP
   module, with zero exceptions).

**What did not change:** `vesper/execution_guard.py` is still the *only*
module that can move money — path 2 is a second way to *call* it, not a
second place the broker-write logic lives. No risk-gate or order-placement
code is duplicated anywhere; `tests/test_trading_mcp.py` pins this
mechanically with an AST walk, not just a docstring promise.

### Pipeline (`vesper/graph.py`)

```
regime_node → scanner_node → analyst_node → playbooks_node → risk_gate_node
    → human_gate_node → (executor_node if approved | reflection_node if not) → reflection_node → END
```

Every node is wrapped so any `audit_trail` entries it returns are committed
immediately to a hash-chained ledger (`vesper/audit_chain.py`, inspected via
`vesper.py audit`) — per-node, not at session end, because `human_gate_node`
can pause the whole graph across a process restart via LangGraph's
`interrupt()`. The graph is compiled with a disk-backed SQLite checkpointer
(`vesper/data/checkpoints.sqlite`) so a paused approval survives a crash.

- **`risk_gate_node`** (`vesper/nodes/risk_gate.py`) runs `RiskEnforcer`
  checks (notional, capital-allocation buckets, sector concentration) against
  live equity/buying-power reads, trips the circuit breaker on a portfolio
  drawdown, and — only after the deterministic checks pass, only if
  `OPENROUTER_API_KEY` is set — runs an LLM red-team audit that may REJECT a
  proposal or halve its quantity, never approve what was rejected or raise
  size (CLAUDE.md rule 6).
- **`human_gate_node`** (`vesper/nodes/human_gate.py`) registers each
  proposal in the disk-backed `ApprovalRegistry`
  (`vesper/bot/inbound.py`), broadcasts a `ProposalCard` to every configured
  channel (`vesper/bot/manager.py`'s `channel_manager`), then either reads
  back an already-resolved decision or calls `interrupt()` to pause the
  graph until one arrives. `ProposalCard` (`vesper/bot/base.py`) carries
  ticker/side/quantity/price, stop/target, thesis, worst-case notional, a
  proposal-time SHA-256 digest, buying-power impact, and the
  before/after allocation-bucket numbers computed one node earlier.
- **`executor_node`** (`vesper/nodes/executor.py`) only acts on
  `prop.approved` proposals. In dry-run mode it writes a simulated fill to
  the paper ledger (and still checks `halt()` explicitly, since it never
  touches `execution_guard` and would otherwise miss the halt check). In
  live mode it calls `execution_guard.guard.preview()` then `.place()`.

### Ticket handshake (`vesper/execution_guard.py`)

The only module allowed to write to a broker. **Preview, then confirm, then
place:**

1. `guard.preview(proposal_id, payload, live_buying_power)` runs the guards
   and, if they pass, stages a `Ticket` — a `uuid4` id plus a SHA-256 digest
   of the exact payload (`hashlib.sha256(json.dumps(payload, sort_keys=True,
   default=str))`).
2. `guard.place(ticket_id, payload, place_fn)` re-hashes the payload it's
   given, refuses if it doesn't match the ticket's stored digest, marks the
   ticket used, and only then calls the broker-specific `place_fn`.

Tickets are **single-use** and **expire after 120 seconds**
(`TICKET_TTL_SEC`); a broker-side rejection un-marks the ticket so a retry
doesn't require re-confirming. `ExecutionGuard` is a process-lifetime
singleton (`guard = ExecutionGuard()`) so a ticket staged by one node
invocation is redeemable by the next.

### Guards, checked on every path

- **`VESPER_TRADING`** — the kill switch. Defaults **off**. Checked before
  anything else, alongside `vesper/halt.py`'s emergency freeze.
- **`VESPER_MAX_NOTIONAL`** (default `2500`) — a SELL-to-open option is
  priced off `strike × 100 × quantity`, not `limit_price`; a payload missing
  `strike` on that path is refused outright rather than under-counted.
- **`VESPER_MAX_QUANTITY`**, **`VESPER_SYMBOL_ALLOWLIST`**,
  **`VESPER_MAX_BP_FRACTION`** — quantity cap, optional allowlist, optional
  buying-power fraction cap.
- **Multi-leg combos** dispatch to a whitelist,
  `_MULTI_LEG_RISK_FORMULAS`, keyed by `strategy_type`. Only two entries
  exist today: `SYNTHETIC_LONG` (long call + short put, same
  strike/expiry/quantity — risk is the put's assignment notional) and
  `THEGA` (fixed-ratio 100 shares : 1 covered call : 3 CSPs, same
  strike/expiry — risk is share cost + all-three-puts-assigned notional).
  An unregistered `strategy_type` is refused outright, never approximated —
  per CLAUDE.md's status section, this means every legged strategy other
  than these two currently has **no reachable success path** in
  production.

A rejection is a `GuardError` (or `TradingDisabled` for the kill
switch/halt case) with a plain-text message, e.g.:

```
order notional ~$75,600.00 exceeds VESPER_MAX_NOTIONAL ($2,500.00).
Raise the cap deliberately if you mean it.
```

---

## Alerts

`alerts.py` + `watcher.py` + `notify.py` — restored 2026-08-29 (CLAUDE.md
rule 4c). Armed and inspected entirely through the CLI, evaluated only while
a `vesper loop` process is running:

```
vesper.py alerts                              # list armed alerts
vesper.py alerts --arm SPY flip below --note "watching for regime flip"
vesper.py alerts --disarm <id>
```

- `LEVEL` is a number **or** one of `alerts.DYNAMIC_LEVELS`: `flip`, `pin`,
  `wall_above`, `wall_below` — re-resolved from TDPro on every evaluation
  (`alerts.resolve_level()`), not frozen at arm time.
- Alerts fire on a **crossing**: one armed on the wrong side of its level
  starts `pending` and only arms once price returns to the expected side. A
  moving level (e.g. the flip drifting past a stationary price) never fires
  on its own — both previous and current price are compared against the
  *current* resolved level (`alerts.evaluate()`).
- Delivery is `notify.Notifier` → ntfy and/or Telegram. The ntfy topic is
  treated as a credential (128 bits of randomness, never surfaced by any
  status output).
- There is no `/api/alerts` route, and arming/disarming is CLI-only — there
  is no MCP tool anywhere that can arm, disarm, or evaluate an alert.
  `trading_mcp/vesper_tools.py`'s `list_alerts` tool (see below) is a
  **read-only** exception: it lists every armed/pending/triggered alert with
  each dynamic level re-resolved live, but cannot create or remove one.
- `Watcher` (`watcher.py`) is a plain background thread started inside
  `vesper loop` (`vesper/alerts_runner.py`), not an asyncio task, because the
  Webull SDK calls it makes are blocking.

---

## MCP tools (`mcp_server/`)

FastMCP server registered under the name `"momentum"`, stdio transport by
default (`MCP_TRANSPORT` env var can switch it to SSE; nothing in this repo
starts it that way). **56 `@mcp.tool` registrations**, all defined in
`mcp_server/server.py` with implementations imported from the other files in
the directory. Holds **no broker credentials**, does not import `wb.py`, and
has **no order-placement tool** — confirmed by reading every file in the
directory, not just the entrypoint. That property is unchanged by Amendment
A4; it's exactly why the order tools were added to a different process
(`trading_mcp/`, documented below) instead of here.

This is a genuinely separate server process from `trading_mcp/` below —
different entrypoint, different auth, different tool count, and **not the
same 56 tools re-exposed**. `trading_mcp/` builds most of its momentum
tooling from a *different* code path, `mcp_server/registry.py`'s
`register_momentum_tools()` (47 tiered tools + 17 TickerTrace `etf_*` tools =
64), which this file's `server.py` does not call — this file defines its own
56 tools inline instead. The two overlap heavily in what they can do, but
are not the same registration, and their counts (56 here vs. 64 of
`trading_mcp/`'s 80) don't correspond 1:1. Don't confuse the two processes:
this one is stdio-only, holds no credentials, and is what a local MCP host
(Claude Code, Claude Desktop) talks to; `trading_mcp/` is the
internet-reachable, owner-only one — see its own section below.

### Screening

| Tool | Required args | Description |
| --- | --- | --- |
| `run_stock_screen` | none (`preset="most_active"`, `limit=25`) | Run a TradingView scanner preset — 22 presets (most_active, new_highs/lows, overbought/oversold, gap_up/down, EMA-stack, pre/after-market movers, etc). |
| `run_custom_screen` | `filters: list[dict]` | Build a custom screen from `{field, operator, value}` conditions over RSI/ADX/ATR/EMA/MACD/BB/Stoch/CCI/volume/etc. |
| `screen_vcp` | none (`tickers`, `max_tickers=50`) | Screen for Volatility Contraction Pattern (Stage 2 tight-base) setups. |
| `screen_pead` | none (`lookback_days=10`) | Screen for Post-Earnings Announcement Drift: gap-up-on-earnings names now pulling back to EMA10/20. |
| `screen_canslim` | none (`tickers`, `max_tickers=30`) | Screen for CANSLIM growth-stock criteria near 52-week highs with institutional sponsorship. |

### Technicals & charts

| Tool | Required args | Description |
| --- | --- | --- |
| `analyze_technicals` | `ticker` | 24 indicators (EMA 8-89, SMA 50/100/200, RSI, MACD, ADX, ATR, Williams %R, Stochastic, Bollinger, CCI) with a summary. |
| `get_tv_analysis` | `ticker` | TradingView 26-indicator consensus (STRONG_BUY…STRONG_SELL) with oscillator/MA counts. |
| `generate_chart` | `ticker` | Candlestick chart with EMA overlays; returns base64 PNG + file path. |
| `analyze_recent_gap` | `ticker` | Scores (0-100) the most recent overnight gap on size, volume, price hold, and fundamentals. |
| `get_momentum_pulse` | none (`tickers`, defaults to 24 warm tickers) | Real-time 0-100 momentum score from EMA-stack alignment, RSI sweet-spot, ADX strength. |

### Options (VoPR™ engine)

| Tool | Required args | Description |
| --- | --- | --- |
| `analyze_options_setup` | `ticker` | Composite realized vol, VRP ratio, Black-Scholes delta/theta, A-F grade for a specific DTE/strike. |
| `find_best_to_sell` | `ticker` | Auto-scans 7-45 DTE puts/calls to sell, scored on RoC/grade/theta/delta; returns top 3 each side. |
| `find_best_to_buy` | `ticker` | Reads technicals for directional bias, scans 21-60 DTE, returns top 3 buys with rationale. |
| `sweep_setups` | none (`tickers`, `max_tickers=10`) | Opportunity board: runs sell+buy scanners across multiple tickers in parallel. |

### Macro, breadth & regime

| Tool | Required args | Description |
| --- | --- | --- |
| `get_exposure_recommendation` | none | Synthesizes VIX, flow, distribution days, trend into a 0-100% capital-deployment ceiling. |
| `get_market_environment` | none | Cross-asset snapshot (equities/bonds/commodities/currencies/crypto) for macro rotation. |
| `detect_macro_regime` | none (`lookback=90`) | Classifies Growth/Inflation/Deflation/Goldilocks regime via RSP/SPY, TLT/SHY, XLY/XLP ratios. |
| `analyze_breadth` | none | 0-100 market breadth score from equal- vs cap-weight trends, new highs/lows, vol term structure. |
| `analyze_uptrend_participation` | none | % of the 11 SPDR sectors + major indices trading above EMA50/EMA200. |
| `detect_themes` | none (`lookback=20`) | Clusters thematic ETFs (AI, Biotech, Energy, …) to find what's moving together. |
| `detect_market_top` | none | O'Neil distribution-day count + defensive-sector-rotation topping signal. |
| `detect_ftd` | none | Detects O'Neil Follow-Through Days confirming a new bull market. |
| `detect_bubble_risk` | none | 0-15 euphoria score from 200d-MA extension, VIX complacency, PE, speculative volume, meme fever. |

### TraderDaddy Pro market intel

| Tool | Required args | Description |
| --- | --- | --- |
| `get_market_pulse` | none | AI-generated options-flow sentiment score, -7 (panic) to +7 (extreme bullish). |
| `get_market_stats` | none | Market-wide put/call ratios and sentiment indicators. |
| `get_put_call_ratios` | none (`ticker="SPY"`) | Put/call ratio for a ticker; <0.7 complacent, >1.0 elevated fear. |
| `get_sector_flow` | none | Sector-by-sector options flow sentiment. |
| `get_unusual_activity` | none | Unusual options flow feed — institutional trades, premium size, conviction. |
| `get_signals` | none | Breakout/continuation signals with technical indicator data. |
| `get_gex_overview` | none | Gamma exposure for SPY/QQQ/IWM; positive = pinning, negative = trending, flip = regime boundary. |
| `get_earnings_calendar` | none | This week's earnings reporters. |
| `get_earnings_flow` | none | Pre-earnings institutional options positioning — market-wide, **not** ticker-filterable (see CLAUDE.md's `get_earnings_flow` gotcha). |
| `get_politician_trades` | none | Congressional stock-trading disclosures. |
| `get_alpha_signals` | none (`ticker`, `signal_type`, `limit=50`) | Recent auto-detected signals (RSI/MACD crosses, volume spikes, EMA breakout, ADX entry) from a background factory scanning 24 tickers every 5-30 min. |

### Research, fundamentals & knowledge base

| Tool | Required args | Description |
| --- | --- | --- |
| `get_fundamentals` | `ticker` | P/E, EPS, revenue growth, margin, short interest, analyst targets, earnings dates, market cap. |
| `get_sec_filings` | `ticker` | SEC EDGAR filing index straight from the primary source (the recent-events sweep). `forms` narrows to types like `["8-K", "10-Q"]`. Needs `SEC_USER_AGENT` (CLAUDE.md). |
| `get_sec_financials` | `ticker` | Multi-period financials from SEC XBRL, including the accrual gap (net income minus operating cash flow — positive means earnings are accrual-driven, not cash-backed). |
| `get_shares_outstanding` | `ticker` | Cover-page share count straight from the 10-Q/10-K, not an aggregator's derived figure; flags an implausible diluted count instead of silently returning it. |
| `get_stakes_held` | `ticker` | AS-FILER 13D/13G — stakes this company holds *in other* public companies, not who owns this ticker. |
| `fetch_ticker_news` | `ticker` | Recent RSS news headlines for a stock. |
| `extract_article_text` | `url` | Full-text extraction of a news article, ads/nav stripped. |
| `search_knowledge` | `query` | RAG search over a 139-book trading-knowledge library. |
| `generate_alpha_card` | `ticker` | Branded HTML analysis card combining technicals + TV consensus for sharing. |

The four EDGAR tools above (`get_sec_filings`, `get_sec_financials`,
`get_shares_outstanding`, `get_stakes_held`) exist in `mcp_server/server.py`
today but were missing from this table before this pass — they are not new
code, just previously undocumented, and are exactly the gap between the old
52 count and the real one.

### Backtesting

| Tool | Required args | Description |
| --- | --- | --- |
| `backtest_strategy` | `ticker` | Backtest one of 6 preset strategies (ema_crossover, rsi_bounce, macd_momentum, bollinger_squeeze, golden_cross, ema_stack_breakout); returns Sharpe/win-rate/CAGR. |
| `sweep_strategy` | `tickers: list[str]` | Runs a strategy across up to 20 tickers, ranked by Sharpe/return. |
| `walk_forward_test` | `ticker` | Walk-forward validation across n folds to detect overfitting. |
| `save_strategy` | `name`, `conditions: dict` | Persists a custom strategy to disk. |
| `list_strategies` | none | Lists saved custom strategies. |
| `get_learned_patterns` | none | Auto-extracted patterns from past backtests with win rates. |

### Misc / journal / sizing

| Tool | Required args | Description |
| --- | --- | --- |
| `get_historical_data` | `ticker` | OHLCV price bars for a ticker/period/interval. |
| `calculate_position_size` | `ticker`, `account_size` | Risk-based sizing via Fixed Fractional, ATR, or Kelly method. |
| `log_conviction` | `ticker`, `direction`, `conviction`, `thesis` | Logs a trade conviction (long/short, high/med/low) to a journal. |
| `get_track_record` | none | Full conviction-journal history with win/loss stats. |
| `analyze_pair` | `ticker_a`, `ticker_b` | Correlation, ratio, and Z-score of the spread between two tickers (stat-arb). |
| `analyze_scenario` | `ticker`, `catalyst` | Bull/base/bear price-target scenarios for a given catalyst. |
| `model_price_distribution` | `ticker` | Confidence-interval (68/95/99%) price targets from historical volatility. |

Full tool count: **56**, verified 2026-09-13 by counting `@mcp.tool`
decorators directly in `mcp_server/server.py`. (An earlier version of this
doc said 52 — undercounting the four EDGAR tools above, not a code change.)

## `trading_mcp/` (owner-only surface)

A **separate process** from `mcp_server/server.py` above — separate
entrypoint (`python -m trading_mcp.server`), separate auth, separate tool
count. It is the deployed, internet-reachable server: 77 read tools plus (as
of Amendment A4, 2026-09-04) 3 order tools, for **80 total**, plus 65
`skill://` resources and 2 prompts. It genuinely holds live Webull, TDPro and
EDGAR credentials, unlike `mcp_server/`.

### Tool composition (80)

| Source | Count | What |
| --- | --- | --- |
| `mcp_server/registry.py`'s `register_momentum_tools()` | 64 | 47 momentum tools (tiers 1-3) + 17 TickerTrace `etf_*` tools. A parallel registration path to `mcp_server/server.py`'s own inline 56 — built for reuse across hosts (this server, `supermcp`), not a re-export of that exact set; tier/tickertrace boundaries don't line up 1:1 with the 56 documented above. |
| `trading_mcp/vesper_tools.py`'s `register_vesper_tools()` | 13 | Read-only Vesper state (see table below). |
| `trading_mcp/order_tools.py`'s `register_order_tools()` | 3 | Order placement, gated by `require_scopes("trade")` (see below). |

The 13 read-only Vesper tools: `get_account_state`, `get_halt_status`,
`get_drawdown_status`, `get_paper_positions`, `get_paper_summary`,
`list_alerts`, `list_pending_proposals`, `get_proposal`, `get_audit_trail`,
`verify_audit_chain`, `get_playbook_calibration`, `recall_similar_setups`,
`get_position_monitor_status`. None of these import `core.halt`'s `halt()`
or `resume()`, or `ApprovalRegistry.submit_decision()` — every one is a pure
read, even the ones (`get_halt_status`, `get_proposal`) that sit right next
to state-changing functions in the same module.

Also registered, separately from the 80 tools: **65 `skill://` resources**
(`trading_mcp/resources.py` — every skill under `skills/` as `skill://<name>`,
plus `skill://rules`) and **2 prompts** (`trading_mcp/prompts.py` —
`copilot_setup`, for the 30-60s voice setup-monitoring cadence, and
`morning_brief`).

### The 3 order tools and the preview → ticket → place handshake

`trading_mcp/order_tools.py`, each carrying `@mcp.tool(auth=require_scopes("trade"))`:

- **`submit_manual_proposal_tool`** — stages an order through the same
  deterministic guards as the human-approval path and returns a `ticket_id`.
  Two-step, matching `execution_guard`'s own preview/place split.
- **`place_from_ticket_tool`** — fires a previously staged ticket. This is
  the second step of the pair above.
- **`place_order_tool`** — stage-and-fire in one call, for when the
  two-step dance isn't needed.

All three funnel into `vesper.execution_guard.guard` — no new broker-write
code, no duplicated risk logic. Rule 3's ticket handshake (single-use,
120-second expiry, payload re-hashed and matched against the ticket's stored
digest before anything reaches the broker) applies identically here.

**The notional cap used to be bypassable through this exact two-step path**
(fixed in the same change that shipped A4): it was enforced only inside
`place_order`, so `submit_manual_proposal_tool` → `place_from_ticket_tool`
reached the broker bounded by nothing but `execution_guard`'s own, much
larger cap. It is now enforced at the single staging chokepoint
(`submit_manual_proposal`) every path shares — pinned by
`test_two_step_path_cannot_bypass_mcp_notional_cap` in
`tests/test_trading_mcp.py`.

### MCP-specific order caps (on top of, not instead of, the guard's own)

- **`MCP_MAX_NOTIONAL`** — an absolute ceiling (default `1000`). Documented
  as exactly that: a ceiling, not the operative cap.
- **`MCP_MAX_NOTIONAL_PCT`** — a fraction of net liquidation value (default
  `0.25`). The *operative* cap is `min(MCP_MAX_NOTIONAL, MCP_MAX_NOTIONAL_PCT
  × NLV)` — the smaller of the two, not the flat constant alone. A flat
  $1000 ceiling on a small live account was 2.5x its buying power, i.e. no
  cap at all in practice.
- **Fails closed.** If NLV cannot be read, the effective cap is **0** and
  every *opening* order is refused — never a silent fallback to the flat
  ceiling. Closing orders skip this cap entirely, since they cannot increase
  exposure.
- **`MCP_MAX_DAILY_ORDERS`** — a plain count limit (default `5`), tracked in
  `trading_mcp/order_tools.py`'s own state file, separate from anything
  `execution_guard.py` tracks.

### Scope model, and the 77-vs-80 behavior

- The static bearer credential (`TRADING_AGENT_TOKEN`) gets scopes
  `["read", "safe-write"]` — **deliberately not `trade`**. A long-lived
  secret sitting in a file on disk cannot place an order under any
  circumstance; only a token minted through the human-present `/authorize`
  OAuth 2.1 gate can carry `trade`.
- Consequence that looks like a bug and is not: an unauthenticated or
  bearer-authenticated `tools/list` returns **77** tools, not 80, and
  calling one of the three order tools with a bearer answers `Unknown tool`
  rather than `403` — FastMCP filters the tool list by scope instead of
  exposing-then-rejecting. Do not "fix" that asymmetry by adding `trade` to
  the bearer's scope list; it is the point.
- `trade` is in the OAuth provider's `default_scopes`, deliberately — the
  claude.ai connector performs dynamic client registration without naming a
  scope, so a `default_scopes=["read"]` would register the connector
  read-only and every order tool would answer 403 even to the owner. The
  actual security boundary is not the scope grant, it's the operator secret
  required at `/authorize` — reaching a `trade`-scoped token still requires
  a human at that gate, and placing an order past that still requires
  `VESPER_TRADING=1`, a clear halt file, an untripped circuit breaker, and
  the MCP caps above.
- **`resume()` and `ApprovalRegistry.submit_decision()` remain unreachable
  from every MCP module — zero exceptions, order tools included.** A tool
  can *originate* an order (`submit_manual_proposal_tool`, `place_order_tool`)
  but can never approve one that's pending in the human-gate path. Voice/chat
  and the Telegram/Discord approval buttons are and remain two different,
  non-overlapping mechanisms. `tests/test_trading_mcp.py` pins this with an
  AST walk over every MCP module's source — not just `ast.Call` nodes, but
  attribute access, import aliases, and dotted paths too, because the live
  order path itself calls `guard.place` by passing the bound method to
  `asyncio.to_thread` rather than invoking it inline, and an earlier version
  of the pin that only matched `ast.Call` would have missed a tool copying
  that exact idiom.

### Network posture

Binds the docker bridge `10.0.0.1:8500` in production — not `0.0.0.0` and
not loopback either, because Traefik is containerised and cannot reach the
host's loopback interface. Traefik terminates TLS at
`https://agent.mphinance.com/mcp`. `MCP_HOST` defaults to `127.0.0.1`, so
reaching anything wider than loopback is always an explicit act, never an
accident of a missing env var. The server refuses to open an HTTP listener
at all — `SystemExit` — with no `TRADING_AGENT_TOKEN`, or with one that
`core/secret_hygiene.py` judges to be a placeholder or low-entropy (CLAUDE.md
rule 2). stdio transport (the default, used by local MCP hosts) needs no
token at all, since stdio carries no headers.

---

## More detail

[CLAUDE.md](../CLAUDE.md) has the design rules behind all of this: the order
path's invariants (rule 3), the LLM narrate/reject-only boundary (rule 6),
push-vs-poll for the monitor (rule 4b), dealer-gamma alert semantics (rule
4c), and the current verified/unverified status of each subsystem.
