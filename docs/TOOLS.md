# MCP tool inventory

Pulled from the live server (`agent.mphinance.com`, `tools/list`) on
2026-09-03. **60 tools registered.**

| group | count | credential | ships publicly |
|---|---|---|---|
| **Free** | **32** | none | ✅ |
| **TMpro** | **15** | `TD_API_KEY` | ✅ (degraded without a key) |
| **Vesper** | **13** | owner-only | ❌ private |

The public repo (`mphinance/momentum-mcp`) ships **47**. The 13 Vesper tools
read live account and agent state and stay private.

---

## Free — 32 tools, no account, no key

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

### Market state
| tool | what it does |
|---|---|
| `detect_market_top` | Distribution days + leadership deterioration |
| `detect_ftd` | Follow-Through Days on major indices |
| `detect_macro_regime` | Growth / Inflation / Deflation / Goldilocks |
| `analyze_breadth` | Breadth health score, 0-100 |
| `analyze_uptrend_participation` | % of market above EMA50/200 |
| `detect_themes` | Trending themes via thematic-ETF clustering |
| `detect_bubble_risk` | Euphoria / bubble score, 0-15 |

### Analysis
| tool | what it does |
|---|---|
| `analyze_pair` | Statistical arbitrage on a pair |
| `analyze_scenario` | Bull/base/bear scenarios around a catalyst |
| `model_price_distribution` | Statistical price targets from historical vol |
| `analyze_recent_gap` | Scores the most recent overnight gap reaction, 0-100 |
| `fetch_ticker_news` | Recent headlines from RSS |
| `extract_article_text` | Full article body, ads and nav stripped |

---

## TMpro — 15 tools, need `TD_API_KEY`

These call `/api/v1/*` on the TraderMatrix Pro backend. Without a key they
return an error; the intent (§4a of the funnel plan) is that they degrade to a
partial result naming what is missing.

### Flow & positioning — 10
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

### Partly TMpro-backed — 5
These work without a key but lose their flow inputs.

| tool | what it does |
|---|---|
| `get_alpha_signals` | Signals from the background signal factory |
| `get_momentum_pulse` | Momentum scores 0-100 — EMA stack, RSI, ADX |
| `screen_pead` | Post-Earnings Announcement Drift setups |
| `get_exposure_recommendation` | Suggested capital deployment, 0-100% |
| `get_market_environment` | Cross-asset environment report |

**Not on this surface:** `apex levels` and `conviction`. Neither has a public
tool module on the key surface — apex is reachable only through a Vespryx
session (see the funnel plan §5.0).

---

## Vesper — 13 tools, private

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

## Note on the two GEX paths

They are different surfaces with different credentials:

| path | credential | anonymous behaviour |
|---|---|---|
| `get_gex_overview` here → `/api/v1/*` | `TD_API_KEY` | no key, no data — `requireApiKey` blocks before any tool runs |
| `get_gex_ticker` in Vespryx's `td-mcp.mjs` | session JWT, optional | **partial data free** — regime, total GEX, spot, `levelCount` |

Only the Vespryx path has an anonymous tier today, and a client bug currently
discards it (`td-api.mjs:327` throws on any `locked:true`, including the
locked-*with-data* envelope). Funnel plan §4a.
