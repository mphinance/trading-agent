# 🗺️ Vesper Engine & Broker Integration Master Roadmap

This document is the authoritative roadmap, architectural specification, and operational execution plan for **Vesper**.

See [`docs/TIERS_AND_FUNNEL.md`](docs/TIERS_AND_FUNNEL.md) for the complete **Starter (Dealer-HUD)** vs. **Pro (TDPro MCP + Vesper)** ecosystem architecture, and [`docs/CODE_SWEEP_2026-08-28.md`](docs/CODE_SWEEP_2026-08-28.md) for code audit findings.

---

## 🔌 Broker Integration Matrix

| Broker | Status | Assets Supported | Auth / Config | Notes |
|---|---|---|---|---|
| **Webull OpenAPI** | ✅ **Active** | Stocks, ETFs, Options, Futures, Crypto | `WEBULL_APP_KEY`, `WEBULL_APP_SECRET` | Official OpenAPI SDK, cash/margin support, 91 MCP tools, guarded by `ExecutionGuard`. |
| **Public.com** | 🟡 **Pre-Wired** | Stocks, ETFs, Options, Crypto, Bonds | `PUBLIC_API_SECRET_KEY`, `PUBLIC_ACCOUNT_ID` | Agentic Brokerage API & Hosted MCP (`https://api.public.com`). Ready to activate when key is provided. |
| **Tradier** | ⚪ **Planned** | Equities, Index Options (XSP / SPX) | `TRADIER_API_KEY` | Dedicated low-latency 0DTE route. |
| **Interactive Brokers (IBKR)** | ⚪ **Planned** | Global multi-asset, Forex, Futures | Client Portal Gateway | Safe "Draft-Only" human UI approval mode. |
| **Alpaca** | ⚪ **Planned** | US Equities, Crypto, Multi-leg Options | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Built-in paper trading sandbox. |

---

## ⚠️ Technical Gotchas (Critical Rules)

- **Synchronous Webull SDK in Async Graph**: `wb.py`'s Webull SDK client is synchronous/blocking. Wrap all blocking Webull SDK calls in `asyncio.to_thread(...)` to prevent stalling the async event loop.
- **Webull Account Rate Limits**: Account and order-query endpoints are limited to **2 req / 2s** (separate from the 600 req/min market data bucket). Reuse `wb.py`'s internal caching.
- **Buying Power Sharing**: Webull shared cash/margin accounts share buying power across accounts — use `max()`, not `sum()`.
- **TraderDaddy Pro Conviction Param**: `get_conviction` takes `symbol`, not `ticker`. Force UTF-8 decoding on API responses.
- **Execution Guardrails Default**: `VESPER_TRADING` defaults to `0` (off). Live order execution requires explicit `VESPER_TRADING=1`.

---

## 🎯 Modular Architecture & Execution Roadmap

### ✅ Module 0: Execution Guardrails & Live Equity Rebuild (Completed)
- [x] **Ticket Handshake**: `preview()` stages a hashed, single-use, 60s time-limited ticket; `place()` verifies the payload SHA-256 before firing.
- [x] **Server-Side Caps**: Notional cap (`VESPER_MAX_NOTIONAL_USD`, default $2,500) and quantity cap (`VESPER_MAX_QUANTITY`, default 100 shares / 10 contracts).
- [x] **Symbol Allowlist**: Optional allowlist (`VESPER_ALLOWED_SYMBOLS`).
- [x] **Kill Switch**: `VESPER_TRADING` (default off) checked before any broker call.
- [x] **Live Equity Integration**: `risk_gate_node` reads live Net Liquidation Value (`nlv`) from Webull with safe fallback.
- [x] **Unit Tests**: 11 unit tests in [`tests/test_execution_guard.py`](tests/test_execution_guard.py) (100% green).

---

### 🌅 Module 1: Automated Pre-Market Battle-Plan Runner (`vesper morning`) — ✅ Landed
- [x] **Macro & Market Health Check**:
  - Query TraderDaddy Pro `get_market_health` (0-7 composite score) and macro regime.
- [x] **Dealer Gamma & Apex Levels**:
  - Query TraderDaddy `levels("SPY")` and `levels("QQQ")` for Spot vs. Gamma Flip line, major open interest magnets, and Apex support/resistance levels.
- [x] **Institutional Whale Flows**:
  - Query TickerTrace Pro `get_briefing` for top smart-money ETF accumulation and cross-fund divergences.
- [x] **Game-Plan Output**:
  - Format concise morning watchlist, 0DTE bias (`BULLISH CALL TRIGGER > $768.62` / `BEARISH PUT TRIGGER < $768.62`), and top 5 momentum candidates with 2x leveraged ETF proxies.

---

### 📱 Module 2: Channel-Agnostic Interactive Live Alert Bot — ✅ Landed
- [x] **Channel-Agnostic Bot Engine**:
  - Defined `ApprovalChannel` interface (`send_proposal_card`, `send_execution_result`, `send_alert`) supporting Discord (`vesper/bot/discord_adapter.py`), Telegram (`vesper/bot/telegram_adapter.py`), and custom Webhooks (`vesper/bot/webhook_adapter.py`).
- [x] **Interactive Visual Cards**:
  - Pushes rich trade proposal cards: Ticker, Direction, Quantity, Limit Price, Max Dollar Risk, Target, Stop-Loss, and quantitative thesis.
- [x] **Execution Callbacks & Multiplexing**:
  - `human_gate_node` and `executor_node` broadcast proposals and execution reports across all configured channels via `ChannelManager`.
- [x] **Unit Tests**:
  - 8 unit tests in [`tests/test_bot_channel.py`](tests/test_bot_channel.py) (100% green).

---

### 🛡️ Module 3: Active Position Monitor & 0DTE Exit Cascade Loop — ✅ Landed
- [x] **Continuous Position Poller**:
  - Background loop (`python vesper.py monitor [--interval 15] [--live] [--once]`) tracking Webull positions within the 2 req / 2s budget.
- [x] **Exit Cascade Rules**:
  - **Hard Take-Profit**: At **+50%** gain, submits sell order for position.
  - **Hard Stop-Loss**: At **-40%** drawdown, immediately triggers emergency stop liquidation.
  - **Trailing Breakeven Lock**: At **+25%**, automatically locks stop-loss to entry price ($0.00 risk).
  - **Time-Based Exit (3:00 PM ET)**: Automatically liquidates 0DTE contracts before final theta/gamma collapse.
  - **Dealer-Gamma Crossing**: Liquidates SPY long calls if spot price breaks below dynamic TraderDaddy Gamma Flip.
- [x] **Unit Tests**:
  - 7 unit tests in [`tests/test_monitor.py`](tests/test_monitor.py) (100% green).

---

### 🧪 Module 4: Walk-Forward Strategy Backtester & Parameter Optimizer
- [ ] **Strategy Presets**:
  - Squeeze Breakout + 8/21 EMA pullback.
  - 0DTE Spot vs. Gamma Flip intraday breakout.
  - High Realized Volatility vs. Implied Volatility (VoPR™ VRP Harvest).
- [ ] **Metrics Generated**:
  - Win Rate (%), Profit Factor, Sharpe Ratio, Max Drawdown (%), Expectancy ($).
- [ ] **Walk-Forward Validation**:
  - In-sample training (2020–2023) vs. out-of-sample validation (2024–2026) to prevent overfitting.

---

### 🧠 Module 5: Conviction Journal Closed-Loop Feedback
- [ ] **Auto-Log on Proposal**:
  - `reflection_node` writes every `OrderProposal` to `data/conviction_journal.json` tagged with source and selected playbook.
- [ ] **Automated Resolution**:
  - Auto-fetch current price after 1/5/10 days to score prediction accuracy and P&L.
- [ ] **Dynamic Strategy Weighting**:
  - Feed resolved hit-rates back into `Candidate.score` and playbook prioritization.

---

### 📈 Module 6: Leveraged ETF Proxy & Skills-Library Scanner Integration
- [ ] **Leveraged Proxy Surfacing**:
  - When high-conviction breakout is identified on an underlying (e.g. `NVDA`), query `data/leveraged_etfs.db` to emit an alternate risk-sized proposal for its 2x vehicle (`NVDL` / `MSTX` / `AVGX`).
- [ ] **Skills as Candidate Sources**:
  - Integrate `skills/vcp-screener`, `skills/momentum-squeeze`, `skills/coil-scan`, and `skills/institutional-flow-tracker` into `scanner_node`.
- [ ] **Autonomous Skill Evolution**:
  - Allow `reflection_node` to propose new `SKILL.md` rules via `vesper/skills_engine.py` when recurring market anomalies are discovered.

---

### 🧯 Module 7: Paper P&L Ledger & Remote Kill Switch
- [ ] **Paper P&L Ledger**:
  - Append every simulated fill to `data/paper_ledger.json` and mark to market daily.
- [ ] **Remote CLI & Chat Kill Switch**:
  - Implement `vesper halt` and `/halt` bot command for instant remote execution freezing.
