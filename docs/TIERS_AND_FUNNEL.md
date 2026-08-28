# 🏆 TraderDaddy Pro & Vesper: Starter vs. Pro Ecosystem

This document defines the dual-tier product architecture uniting the **Dealer-HUD Chrome Extension** (Starter / Visual Tier) and the **TDPro MCP + Vesper Execution Engine** (Pro / AI Agent Tier).

---

## 🏛️ Ecosystem Overview

```
                  ┌────────────────────────────────────────────────────────┐
                  │               TraderDaddy Pro User Journey             │
                  └───────────────────────────┬────────────────────────────┘
                                              │
                   ┌──────────────────────────┴──────────────────────────┐
                   ▼                                                     ▼
    ┌───────────────────────────────┐                     ┌───────────────────────────────┐
    │     Level 1: Visual Starter   │                     │      Level 2: AI Agent Pro    │
    │    (Dealer-HUD Extension)     │                     │      (TDPro MCP + Vesper)     │
    ├───────────────────────────────┤                     ├───────────────────────────────┤
    │ • Eyes on the Chart           │                     │ • Brain in the Machine        │
    │ • Paints walls on TradingView │                     │ • Connects to Claude & Vesper │
    │ • Net Gamma Flip line         │                     │ • 29 Quant MCP Tools          │
    │ • Verdict Panel (Call/Put)    │                     │ • Autonomous Scanning & GEX   │
    │ • Discretionary Execution     │                     │ • Webull & Public.com 1-Click │
    └───────────────────────────────┘                     └───────────────────────────────┘
```

---

## 🎯 Tier Breakdown

### 👁️ Level 1: Starter / Visual Tier (Dealer-HUD)
* **Delivery**: Chrome Extension ([`dealer-hud`](https://github.com/mphinance/dealer-hud)).
* **Target**: Discretionary traders who trade manually on TradingView.
* **Features**:
  * Injects live dealer walls (Resistance, Support, Pin) onto TradingView charts.
  * Draws the Net Gamma Flip line with open interest overlays.
  * In-chart **Verdict Panel**: Real-time signal reading (`CONSIDER BUYING CALLS`, `CONSIDER HEDGING WITH PUTS`, `DO NOTHING HERE`).
  * Free / Starter entry point that proves data edge immediately on the user's screen.

---

### 🧠 Level 2: Pro / AI Quant Tier (TDPro MCP + Vesper)
* **Delivery**: Whop Membership API / MCP Key + Vesper LangGraph Engine.
* **Target**: Quantitative traders, algorithmic funds, and AI power users.
* **Features**:
  * **Direct Headless MCP Access**: 29 tools covering Net Dealer Gamma (GEX), Market Health (0-7 composite score), Apex levels, Dark Pool, Unusual Activity, and Politician Trades.
  * **Vesper Stateful Execution Engine**: 8-node LangGraph agent with deterministic 2% zero-loss risk guardrails, 0DTE flow cascade, and Minervini VCP momentum screens.
  * **Direct Broker Execution**: 1-click execution through Webull OpenAPI and pre-wired Public.com Agentic Brokerage.
  * **Conviction Journal**: Metacognitive long-term memory calibration tracking win rates over time.

---

## 📈 Conversion Loop

1. **Top of Funnel**: User discovers Dealer-HUD and sees live dealer levels drawn directly on TradingView.
2. **Value Proof**: User verifies that Gamma Flip levels and Apex pins reliably act as market support/resistance.
3. **Upgrade Trigger**: User wants automated setup discovery, 0DTE trade planning, and AI agent execution without watching charts all day $\rightarrow$ Upgrades on Whop to unlock **TDPro API & MCP Access**.
4. **Retention**: Vesper runs daily scans and pushes high-conviction trade cards for 1-click execution.
