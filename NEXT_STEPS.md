# 🚀 Vesper: Next Operational Modules Specification

This document details the architectural design and execution plans for the next 4 modules of **Vesper**.

---

## 1. 🌅 Module 1: Automated Pre-Market Battle-Plan Runner (`vesper morning`)

### Objective
Deliver an automated, high-density market briefing every morning at **8:45 AM ET** before the opening bell, combining macro posture, dealer gamma positioning, institutional ETF flows, and key setup candidates.

### Workflow & Architecture
1. **Macro & Market Health Check**:
   - Query TraderDaddy Pro `get_market_health` for overall composite score (0-7 scale).
   - Check US macro regime transition status (`detect_macro_regime`).
2. **Dealer Gamma (GEX) & Apex Levels**:
   - Query TraderDaddy `levels("SPY")` and `levels("QQQ")` for:
     - Spot price vs. Net Gamma Flip line
     - Major call/put open interest pins ("Magnets")
     - Apex support/resistance levels
3. **Institutional Whale Flows & Pre-Market Briefing**:
   - Query TickerTrace Pro `get_briefing` for top smart-money ETF accumulation and cross-fund divergences.
4. **Actionable Game-Plan Output**:
   - Formats a clean terminal/markdown output containing:
     - 0DTE Bias: `BULLISH CALL TRIGGER > $768.62` / `BEARISH PUT TRIGGER < $768.62`
     - Top 5 Volatility Squeeze / VCP momentum candidates
     - Key levels to watch for the session.

---

## 2. 📱 Module 2: Telegram / Discord Interactive Live Alert Bot

### Objective
Enable mobile trade approval and real-time alerts. Whenever Vesper generates a high-conviction trade proposal, it pushes an interactive card directly to your private phone channel with 1-click execution callbacks.

### Workflow & Architecture
1. **Bot Engine**:
   - Lightweight asynchronous bot (`python-telegram-bot` or `discord.py`) integrated into Vesper's event runner.
2. **Interactive Approval Card**:
   - Pushes visual trade details:
     ```
     ⚡ VESPER TRADE PROPOSAL [High Conviction]
     -----------------------------------------
     Ticker: SPY (0DTE Option)
     Action: BUY 1x 770 CALL @ $1.80
     Est. Cost: $180.00 | Max Risk: $72.00 (-40%)
     Target: $2.70 (+50%) | Time-Stop: 3:00 PM ET
     Thesis: Spot ($769.35) > Gamma Flip ($768.62)
     
     [ ✅ APPROVE & EXECUTE ]   [ ❌ REJECT / ABORT ]
     ```
3. **Execution Callback**:
   - Tapping **`[ Approve ]`** calls Webull OpenAPI (`wb.trade.order_v2.place_order`) and responds immediately with fill confirmation and order ID.
   - Tapping **`[ Reject ]`** marks the proposal as rejected and logs the rationale to the conviction memory journal.

---

## 3. 🛡️ Module 3: Active Position Monitor & 0DTE Exit Cascade Loop

### Objective
A continuous background loop (every 15–30 seconds during market hours: 9:30 AM – 4:00 PM ET) that tracks open positions on Webull and strictly enforces deterministic exit rules.

### Workflow & Architecture
1. **Position Poller**:
   - Queries Webull account positions (`get_account_positions`) without exceeding the 2 req / 2 sec trade rate limit bucket.
2. **Exit Cascade Rules Enforced**:
   - **Hard Take-Profit**: At **+50%** gain, submits limit/market sell order for 50-100% of the position.
   - **Hard Stop-Loss**: At **-40%** drawdown, immediately submits market stop order to prevent catastrophic zero-DTE decay.
   - **Time-Based Exit (3:00 PM ET)**: Automatically closes all 0DTE contracts before final 60-minute volatility spikes.
3. **Trailing Breakeven Lock**:
   - Once a position crosses **+25%**, the stop-loss automatically ratchets up to entry price ($0.00 risk).

---

## 4. 🧪 Module 4: Walk-Forward Strategy Backtester & Parameter Optimizer

### Objective
Systematically stress-test our core playbooks (Minervini VCP, Bullish EMA Momentum Stack, and VoPR™ Options Pricing) across historical market cycles.

### Workflow & Architecture
1. **Strategy Presets**:
   - Squeeze Breakout + 8/21 EMA pullback
   - 0DTE Spot vs. Gamma Flip intraday breakout
   - High Realized Volatility vs. Implied Volatility (VRP Harvest)
2. **Metrics Generated**:
   - Win Rate (%)
   - Profit Factor & Sharpe Ratio
   - Maximum Drawdown (%)
   - Expectancy per trade ($)
3. **Walk-Forward Validation**:
   - Trains/optimizes hyperparameters on in-sample windows (e.g. 2020–2023) and validates out-of-sample (2024–2026) to prevent overfitting.
