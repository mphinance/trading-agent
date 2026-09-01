# supermcp vs. Vesper — MCP tool & strategy inventory (2026-09-01)

Snapshot comparison to inform the "fold Vesper into supermcp" decision. Vesper's tools
are `mcp_server/server.py` (run locally as the `momentum` connector in `~/.claude.json`,
56 tools). supermcp's tools are `src/app.py` on the live `vultr` box (37 tools, connector
`https://mcp.mphinance.com/mcp`).

## Identity

| | Vesper (`mcp_server/`) | supermcp |
|---|---|---|
| Purpose | Pure read-only quant/data tooling | Federated account gateway: Tastytrade + Substack + Webull, live order routing |
| Auth | None (local stdio) | `MultiAuth` — static bearer + OAuth 2.1 |
| Holds broker creds? | No (by design — rule 3) | Yes (Tastytrade, Webull, Substack) |
| Can place live orders? | No | Yes — `place_live_order`, `LIVE_ORDERS_ENABLED=True` today |
| Runs where | Locally, stdio, launched per-session | `vultr`, systemd, always-on |

## Tool inventory — Vesper's 56

**Technicals / charting (7)** — `run_stock_screen`, `run_custom_screen`,
`get_historical_data`, `analyze_technicals` (24 indicators), `get_tv_analysis`
(TradingView 26-indicator consensus), `generate_chart`, `calculate_position_size`

**Options analytics (4)** — `analyze_options_setup` (VoPR engine), `find_best_to_sell`,
`find_best_to_buy`, `sweep_setups`

**Fundamentals / SEC EDGAR (5)** — `get_fundamentals`, `get_sec_filings`,
`get_sec_financials`, `get_shares_outstanding`, `get_stakes_held`

**News (3)** — `fetch_ticker_news`, `extract_article_text`, `generate_alpha_card`

**Knowledge / journal (3)** — `search_knowledge` (139-book library),
`log_conviction`, `get_track_record`

**Backtesting (6)** — `backtest_strategy`, `save_strategy`, `list_strategies`,
`get_learned_patterns`, `sweep_strategy`, `walk_forward_test`

**Market sentiment / flow (11)** — `get_market_pulse`, `get_market_stats`,
`get_put_call_ratios`, `get_sector_flow`, `get_unusual_activity`, `get_signals`,
`get_gex_overview`, `get_earnings_calendar`, `get_earnings_flow`,
`get_politician_trades`, `get_alpha_signals`

**Screeners (3)** — `screen_vcp`, `screen_pead`, `screen_canslim`

**Regime / breadth / macro (14)** — `detect_market_top`, `detect_ftd`, `analyze_pair`,
`analyze_scenario`, `model_price_distribution`, `get_exposure_recommendation`,
`get_market_environment`, `detect_macro_regime`, `analyze_breadth`,
`analyze_uptrend_participation`, `detect_themes`, `analyze_recent_gap`,
`detect_bubble_risk`, `get_momentum_pulse`

**Zero tools for:** account balance, positions, orders, journal/ledger, alerts, halt
status. That's deliberate — this server has never touched the account (rule 3).

## Tool inventory — supermcp's 37

**Account / broker state (9)** — `get_balance`, `get_positions`, `get_holdings`,
`set_wheel_state`, `get_wheel_states`, `get_trade_journal`, `get_ledger`,
`get_returns`, `get_trade_stats`

**Writing / Substack (7)** — `list_writings`, `search_writings`, `lookup_ticker`,
`true_up`, `trade_guide`, `draft_from_position`, `promote_to_draft`

**Order / execution (3)** — `ticket_from_writing`, `dry_run_order`,
**`place_live_order`** (the only tool in either server that can route a real fill)

**Notify (2)** — `send_trade_alert`, `send_holdings_dash`

**Screening (5)** — `screen_presets`, `prescan`, `screen_analytics` (StrikeForge
factor ranking), `gamma_flush_scan` (the one real scan-and-propose setup),
`scan_history` (falsifiability log)

**TDPro data (11)** — `tdpro_flow`, `tdpro_flow_summary`, `tdpro_market_pulse`,
`tdpro_gex`, `tdpro_screener`, `tdpro_signals`, `tdpro_smart_money`, `tdpro_ticker`,
`tdpro_quality`, `tdpro_options`, `tdpro_pine`

**Zero tools for:** technical indicators beyond StrikeForge's factors, fundamentals/SEC,
news, backtesting, macro regime, breadth, options VoPR analytics, knowledge-base search.

## Overlap

Both wrap TDPro, independently — Vesper's is broader per-call (gamma, flow, earnings
flow, politician trades, unusual activity, sector flow, signals all separate tools),
supermcp's `tdpro_*` set is thinner but includes `tdpro_pine` (Pine script generation)
and `tdpro_quality` (Long-Term Quality score), which Vesper doesn't have. Running both
connectors at once means asking the same TDPro data two different ways.

## Trading strategies (the actual "scan for me" question)

**Vesper's `playbooks_node` — 8 named playbooks**, all producing sized order
proposals gated by `risk_gate_node` before reaching approval:

1. **0DTE Flow** (SPY/QQQ) — dealer-gamma-flip directional play, IV≥70% gate,
   Black-Scholes 0.30-delta strike selection with major-OI-wall fallback
2. **Tao of Trading Bounce 2.0 & Momentum Pullback** — technical bounce/continuation
3. **Collar-Following** — replicates income-ETF protective-put structure
4. **ADX/IV Option-Style Router** — picks option structure by trend strength + vol regime
5. **THEGA** — delta-neutral volatility harvest (multi-leg)
6. **Premium-Recycling "Free Share" Engine** — covered-call premium compounding
7. **Tax Reserve Sweep** — 25% of realized P&L → $SGOV
8. **Earnings-Week CSP Vega Harvest** — cash-secured puts timed to earnings IV crush

**supermcp — 1 real scan-and-propose setup:** `gamma_flush_scan` /
`wallscan.py` — gamma-flush-into-dealer-support-wall, with entry/stop/target/R:R and
a live 1-min-bar trigger check (`approaching`/`at_wall`/`reclaimed`/`broken`). Logged
to `scan_history` for grading. `screen_presets`/`prescan` rank a universe by factor
score but don't produce a sized, triggered trade proposal the way `wallscan` or
Vesper's playbooks do.

## Bottom line

- **Scanning breadth**: Vesper 8 playbooks + ~14 regime/breadth/screener tools vs.
  supermcp's 1 playbook + factor ranking. This is the real gap for "I'm not staring at
  80 charts" — supermcp today only ever surfaces the gamma-flush setup on its own.
- **Data breadth**: Vesper's `mcp_server/` has ~5x supermcp's tool count, almost
  entirely non-overlapping categories (backtesting, SEC fundamentals, VoPR options
  analytics, macro regime, news) that supermcp has no equivalent for.
- **Execution**: only supermcp can place a live order today; Vesper's `mcp_server/`
  was never wired to touch the account at all.
- **Risk sizing**: neither server's *scanning* tools apply portfolio-level risk
  (buckets, circuit breaker, sector concentration) — that logic lives in Vesper's
  `risk_gate_node`/`risk.py`, downstream of the playbooks, not in either MCP tool set.
