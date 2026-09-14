# MCP tool inventory

Verified against `trading_mcp/server.py` by importing it on **2026-09-13**
(counts below updated 2026-09-14 for `get_iv_rank` + `reprice_option` + `classify_iv_rank`).
**83 tools registered.**

| group | count | credential | ships publicly |
|---|---|---|---|
| **Free** | **37** | none | ✅ |
| **TickerTrace `etf_*`** | **17** | none | ✅ |
| **TMpro** | **13** | `TD_API_KEY` | ✅ (degraded without a key) |
| **Vesper (read)** | **13** | owner-only | ❌ private |
| **Order path** | **3** | owner-only **+ `trade` OAuth scope** | ❌ private |

The first 67 are general-purpose market tooling and would work for anybody. The
last 16 only mean anything if you hold the account.

> **An unauthenticated or bearer-token `tools/list` returns 80, not 83.** The
> three order tools carry `require_scopes("trade")`, and FastMCP *filters* by
> scope rather than returning a 403 — so calling one with the static bearer
> answers `Unknown tool`. That looks like a broken deploy and isn't. See
> [`CONNECTOR_AUTH.md`](CONNECTOR_AUTH.md).

Two servers register from the same registry and should not be confused:

| server | process | tools | can it place an order? |
|---|---|---|---|
| `mcp_server/server.py` | stdio "momentum" | 56 | **No.** Holds no broker credentials and has no order path. That property is load-bearing. |
| `trading_mcp/server.py` | owner-only, deployed | **83** | Yes, through three scope-gated tools. |

---

## Free — 37 tools, no account, no key

yfinance, TradingView, SEC EDGAR and local computation. An MCP server runs on
the caller's machine, so these cost nothing to serve.

### Fundamentals & filings
| tool | what it does |
|---|---|
| `get_fundamentals` | P/E, EPS, revenue growth, margin, short interest |
| `get_sec_filings` | SEC EDGAR filing index, primary source |
| `get_sec_financials` | Multi-period XBRL financials, including the accrual gap |
| `get_shares_outstanding` | Cover-page share count from the 10-Q/10-K |
| `get_stakes_held` | AS-FILER 13D/13G — stakes this company holds in others |

### Screening
| tool | what it does |
|---|---|
| `run_stock_screen` | TradingView scanner, 22 presets |
| `run_custom_screen` | Custom screen with dynamic filter conditions |
| `screen_vcp` | Volatility Contraction Pattern |
| `screen_canslim` | CANSLIM growth criteria |

### Technicals & charts
| tool | what it does |
|---|---|
| `analyze_technicals` | 24 indicators — EMA stack, RSI, MACD, ADX, ATR, Bollinger |
| `get_tv_analysis` | TradingView 26-indicator consensus |
| `get_momentum_pulse` | Momentum scores 0-100 — EMA stack, RSI, ADX, computed locally |
| `get_historical_data` | OHLCV price history |
| `generate_chart` | Candlestick chart with EMA overlays (8/21/34/55/89) |
| `generate_alpha_card` | Shareable HTML card combining technicals + TV analysis |

### Options analytics
| tool | what it does |
|---|---|
| `analyze_options_setup` | VoPR™ — composite realized vol (4 estimators), VRP ratio, Delta/Theta, A-F grade |
| `find_best_to_sell` | Best puts and calls to sell, 7-45 DTE |
| `find_best_to_buy` | Best directional option to buy, 21-60 DTE |
| `sweep_setups` | Opportunity board across multiple tickers |
| `calculate_position_size` | Fixed-fractional, ATR or Kelly sizing |
| `reprice_option` | Guesstimate a contract's price at a different spot (e.g. premarket) and/or IV, via Black-Scholes |
| `classify_iv_rank` | Fast wheel-vs-buy verdict for a 0-100 IV rank you already have (pure lookup, no credential) — ICE_COLD/CHEAP favor buying long options, ABOVE_AVERAGE/RICH/SCORCHING favor selling premium |

### Market state
| tool | what it does |
|---|---|
| `detect_ftd` | Follow-Through Days on major indices |
| `detect_macro_regime` | Growth / Inflation / Deflation / Goldilocks |
| `analyze_breadth` | Breadth health score, 0-100 |
| `analyze_uptrend_participation` | % of market above EMA50/200 |
| `detect_themes` | Trending themes via thematic-ETF clustering |
| `detect_bubble_risk` | Euphoria / bubble score, 0-15 |
| `get_exposure_recommendation` | Suggested capital deployment, 0-100% |
| `get_market_environment` | Cross-asset environment report |

### Analysis
| tool | what it does |
|---|---|
| `analyze_pair` | Statistical arbitrage on a pair |
| `analyze_scenario` | Bull/base/bear scenarios around a catalyst |
| `model_price_distribution` | Statistical price targets from historical vol |
| `analyze_recent_gap` | Scores the most recent overnight gap reaction, 0-100 |
| `get_alpha_signals` | Signals from the background signal factory |
| `fetch_ticker_news` | Recent headlines from RSS |
| `extract_article_text` | Full article body, ads and nav stripped |

---

## TickerTrace — 17 `etf_*` tools, no credential

Institutional positioning derived from daily ETF holdings: **71 tracked funds**
over **~2,270 underlyings**, served by `api.tickertrace.pro`. Registered by
default (`register_momentum_tools(..., include_tickertrace=True)`).

| tool | what it does |
|---|---|
| `etf_briefing` | Pre-market institutional briefing: top buys and sells, multi-provider |
| `etf_signals` | Conviction-scored institutional buy/sell signals from daily holdings |
| `etf_institutional_flow` | Aggregate accumulation/distribution across all active-equity funds |
| `etf_institutional_trend` | Per-ticker accumulation/distribution trend by day, week, month |
| `etf_holdings_changes` | Raw position changes, filterable by provider |
| `etf_divergences` | Where different funds traded the SAME name in opposite directions |
| `etf_layering_patterns` | 3+ independent stock-pickers opening the same new position in a window |
| `etf_sector_flow` | Sector-level inflows and outflows from fund holdings |
| `etf_stock_activity` | Everything institutional for one stock: who holds it, how weights moved |
| `etf_fund_detail` | One ETF's top holdings, options count, AUM |
| `etf_list_funds` | All 71 tracked funds with holdings counts and top positions |
| `etf_list_tickers` | The most widely-held underlyings across every tracked fund |
| `etf_income_overview` | Option-income funds by structure: covered-call, synthetic, leap-proxy, swap |
| `etf_income_fund_detail` | One income fund's full book: call coverage, moneyness, overlay |
| `etf_options_listings` | CBOE daily diff: newly optionable stocks and new weekly listings |
| `etf_signal_performance` | Historical backtest of the TickerTrace conviction signals |
| `etf_global_stats` | Coverage stats: funds, underlyings, options contracts |

---

## TMpro — 13 tools, need `TD_API_KEY`

These reach the TraderDaddy Pro backend (marketed as TraderMatrix Pro; same
service). Without a key they degrade rather than crash.

### Flow & positioning — 11, directly TDPro-backed
| tool | what it does |
|---|---|
| `get_gex_overview` | Gamma exposure for SPY/QQQ/IWM. Flip level = regime boundary |
| `get_unusual_activity` | Unusual options flow — institutional trades, premium, conviction |
| `get_sector_flow` | Sector-by-sector options flow with sentiment |
| `get_market_pulse` | AI market sentiment with options-flow score (-7 to +7) |
| `get_market_stats` | Market-wide put/call ratios and sentiment |
| `get_put_call_ratios` | Put/call for SPY, QQQ, IWM or any ticker |
| `get_signals` | Breakout and continuation signals |
| `get_earnings_calendar` | Who reports this week |
| `get_earnings_flow` | Pre-earnings institutional positioning |
| `get_politician_trades` | Congressional disclosures |
| `get_iv_rank` ⚠️ | Self-relative IV rank (0-100), rich vs. cheap premium — **registered but not yet wired**: no working `/api/v1` REST path has been found (every plausible one 404s); it fails soft with an explicit error until someone supplies the real endpoint. See `core/traderdaddy.py`'s `get_iv_rank` docstring. |

### Indirectly TDPro-backed — 2
These import a module that reaches TDPro, so they lose an input without a key.

| tool | via |
|---|---|
| `screen_pead` | `mcp_server/pead_screener.py` |
| `detect_market_top` | `core/market_top.py` |

> **Corrected 2026-09-13.** This section previously listed 15 and named
> `get_alpha_signals`, `get_momentum_pulse`, `get_exposure_recommendation` and
> `get_market_environment` as "partly TMpro-backed". Traced through the imports,
> none of `mcp_server/warmer.py`, `mcp_server/exposure.py` or
> `mcp_server/environment.py` touches `core/traderdaddy` — those four are free
> tools and are listed above.

**Not on this surface:** `apex levels` and `conviction`. Neither has a public
tool module on the key surface — apex is reachable only through a Vespryx
session (see the funnel plan §5.0).

---

## Vesper — 13 read tools, private

Read-only views over the trading agent's own state. They touch live account
data, the approval queue and the audit ledger, so they never ship publicly.

| tool | what it does |
|---|---|
| `get_account_state` | Live equity, buying power, open positions |
| `get_halt_status` | Whether the emergency freeze is engaged, and why |
| `get_drawdown_status` | Circuit breaker: tracked peak NLV vs configured limit |
| `get_paper_positions` | Open simulated positions |
| `get_paper_summary` | Paper NLV, realized/unrealized P&L, win rate |
| `list_alerts` | Armed/pending/triggered alerts with resolved dynamic levels |
| `list_pending_proposals` | Orders awaiting a human's Telegram/Discord tap |
| `get_proposal` | One proposal's record and how it was acted on |
| `get_audit_trail` | Recent entries in the hash-chained audit ledger |
| `verify_audit_chain` | Walk the chain, localise any broken hash link |
| `get_playbook_calibration` | Resolved win rate and calibration for a playbook |
| `recall_similar_setups` | Semantic recall of similar historical setups |
| `get_position_monitor_status` | What the exit cascade would do to each open position |

---

## Order path — 3 tools, private, `trade` scope

**Live since Amendment A4 (2026-09-04).** Registered by
`trading_mcp/order_tools.py`, each decorated `@mcp.tool(auth=require_scopes("trade"))`.
They reach the broker through `vesper.execution_guard` and nothing else.

| tool | what it does |
|---|---|
| `submit_manual_proposal_tool` | Stage an order through the guards; returns a `ticket_id` |
| `place_from_ticket_tool` | Fire a previously staged ticket by id |
| `place_order_tool` | One-call placement under the stricter MCP limits |

The two-step path exists so that no single call can both construct and fire an
order. `preview()` stages a ticket carrying a SHA-256 of the exact payload;
`place()` takes a ticket id, never an order, so what was approved is
byte-for-byte what reaches the broker. Tickets are single-use and expire in
120s.

**What bounds these, all enforced in code:**

- `VESPER_TRADING` — kill switch, defaults **off**.
- `core/halt.py` — emergency freeze, checked before anything else.
  `core/circuit_breaker.py` trips it automatically on a 15% trailing-peak NLV
  drawdown.
- The MCP notional cap — `min(MCP_MAX_NOTIONAL, MCP_MAX_NOTIONAL_PCT × NLV)`,
  enforced at the single staging chokepoint every path shares, and it **fails
  closed**: if NLV cannot be read the cap is 0 and every opening order is
  refused. Closing orders skip it, since they cannot increase exposure.
- `MCP_MAX_DAILY_ORDERS`.
- The guard's own caps in `vesper/execution_guard.py` — notional, quantity,
  optional symbol allowlist, optional buying-power fraction.

**What these tools cannot do.** `resume()` and
`ApprovalRegistry.submit_decision()` are unreachable from every MCP module,
with zero exceptions, pinned mechanically by an AST walk in
`tests/test_trading_mcp.py`. So a tool call can *originate* an order and can
never *approve* a pending one. Approval is an inline Telegram or Discord
button. `vesper/execution_guard.py` remains the only module in the repo that
can move money.

---

## Also registered: 65 skill resources and 2 prompts

Not tools, and easy to miss in a `tools/list`.

- **65 MCP resources** at `skill://…` (`trading_mcp/resources.py`) — playbooks a
  client can load instead of re-deriving method in the prompt: 0DTE flow rules,
  VCP and CANSLIM screening, breadth and regime frameworks, position sizing,
  dividend-growth process, the edge-research pipeline. `skill://rules` is the
  operating contract for the server itself.
- **2 prompts** (`trading_mcp/prompts.py`) — `morning_brief` for pre-market, and
  `copilot_setup`, designed to be invoked on a 30-60 second cadence for live
  setup monitoring.

---

## Note on the two GEX paths

They are different surfaces with different credentials:

| path | credential | anonymous behaviour |
|---|---|---|
| `get_gex_overview` here → `/api/v1/*` | `TD_API_KEY` | no key, no data — `requireApiKey` blocks before any tool runs |
| `get_gex_ticker` in Vespryx's `td-mcp.mjs` | session JWT, optional | **partial data free** — regime, total GEX, spot, `levelCount` |

Only the Vespryx path has an anonymous tier today, and a client bug currently
discards it (`td-api.mjs:327` throws on any `locked:true`, including the
locked-*with-data* envelope). Funnel plan §4a.

Whichever path you use: dealer gamma is a map of positioning, not a forecast.
It marks where hedging is concentrated, which is why price often *reacts*
there. It never means price will travel there. See CLAUDE.md rule 4a.
