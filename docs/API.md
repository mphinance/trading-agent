# Vesper — operational reference

This doc used to describe three surfaces: MCP tools as a thin HTTP client, a
42-route FastAPI app (`server.py`, `:8787`), and an `/api/stream` SSE feed.
Commit `de60d51` deleted that browser-dashboard architecture along with
`server.py`, `static/index.html`, `chat.py` and `orders.py` — see CLAUDE.md's
"History that will otherwise confuse you" for the full story. None of it is
coming back. This is a from-scratch rewrite describing what is actually here
today. See [README.md](../README.md) for setup/run instructions — this doc is
the detailed reference for the CLI, the MCP tool inventory, and the order
path; it doesn't repeat what the README already covers.

## What exists now

| Surface | What it is | Can place an order? |
| --- | --- | --- |
| **CLI** (`vesper.py`) | The operational surface. Scan/analyze/monitor/loop/listen/alerts/halt/status/paper/audit. | Only via the LangGraph pipeline + a human approval tap. |
| **MCP server** (`mcp_server/`, entrypoint `mcp_server/server.py`) | Read-only quant tooling — screeners, technicals, options analytics, macro/breadth, research, backtesting — exposed to MCP hosts over stdio (FastMCP; `MCP_TRANSPORT=sse` is supported by the code but nothing in this repo starts it that way). | **No.** No broker credentials, no `wb.py` import, no order-placement tool anywhere in the directory. |
| **Telegram / Discord bots** (`vesper/bot/`) | Outbound-only approval channels — long-poll (Telegram) / gateway connection (Discord). Render proposal cards, resolve Approve/Reject taps, accept `/halt` and `/resume`. | Indirectly — a tap resolves a paused graph node, which is the only thing that can reach `executor_node`. |
| **HTTP** | **Nothing serves it.** `vesper/bot/inbound.py` defines `create_inbound_app()` (an aiohttp app with `/webhook/telegram`, `/webhook/discord`, `/webhook/approval`, `/health`, `/approvals`), but nothing in this repo calls it or runs `web.run_app()` on it. It exists as an alternative approval-delivery mechanism that was never wired up — see rule 1 in CLAUDE.md. | N/A — not running. |

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

There is no MCP tool and no HTTP route that can place an order. The only way
one reaches Webull is: the LangGraph pipeline drafts a proposal → the
deterministic risk gate passes it → a human taps Approve on a Telegram or
Discord card → `executor_node` calls `vesper/execution_guard.py`.

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
- There is no `/api/alerts` route and no MCP alert tools — `Watcher`
  (`watcher.py`) is a plain background thread started inside `vesper loop`
  (`vesper/alerts_runner.py`), not an asyncio task, because the Webull SDK
  calls it makes are blocking.

---

## MCP tools (`mcp_server/`)

FastMCP server registered under the name `"momentum"`, stdio transport by
default (`MCP_TRANSPORT` env var can switch it to SSE; nothing in this repo
starts it that way). **52 `@mcp.tool` registrations**, all defined in
`mcp_server/server.py` with implementations imported from the other files in
the directory. Holds **no broker credentials**, does not import `wb.py`, and
has **no order-placement tool** — confirmed by reading every file in the
directory, not just the entrypoint.

(The server's own internal strings are stale and disagree with each other —
the FastMCP `instructions` text says "33 quantitative trading tools," the
startup log line says "35 tools registered." The actual count, from the
`@mcp.tool` decorators themselves, is 52.)

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
| `fetch_ticker_news` | `ticker` | Recent RSS news headlines for a stock. |
| `extract_article_text` | `url` | Full-text extraction of a news article, ads/nav stripped. |
| `search_knowledge` | `query` | RAG search over a 139-book trading-knowledge library. |
| `generate_alpha_card` | `ticker` | Branded HTML analysis card combining technicals + TV consensus for sharing. |

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

Full tool count: **52**, verified against `mcp_server/server.py`'s
`@mcp.tool` decorators (no discrepancy from the count above).

## More detail

[CLAUDE.md](../CLAUDE.md) has the design rules behind all of this: the order
path's invariants (rule 3), the LLM narrate/reject-only boundary (rule 6),
push-vs-poll for the monitor (rule 4b), dealer-gamma alert semantics (rule
4c), and the current verified/unverified status of each subsystem.
