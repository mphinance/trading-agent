# 🗺️ Vesper Engine & Broker Integration Roadmap

This document outlines the planned expansion of execution routes, market intelligence data streams, and autonomous features for **Vesper**.

See [`docs/TIERS_AND_FUNNEL.md`](docs/TIERS_AND_FUNNEL.md) for the complete **Starter (Dealer-HUD)** vs. **Pro (TDPro MCP + Vesper)** ecosystem architecture.

---

## 🔌 Broker Integration Matrix

| Broker | Status | Assets Supported | Auth / Config | Notes |
|---|---|---|---|---|
| **Webull OpenAPI** | ✅ **Active** | Stocks, ETFs, Options, Futures, Crypto | `WEBULL_APP_KEY`, `WEBULL_APP_SECRET` | Official OpenAPI SDK, cash/margin support, 91 MCP tools. |
| **Public.com** | 🟡 **Pre-Wired** | Stocks, ETFs, Options, Crypto, Bonds | `PUBLIC_API_SECRET_KEY`, `PUBLIC_ACCOUNT_ID` | Agentic Brokerage API & Hosted MCP (`https://api.public.com`). Ready to activate when key is provided. |
| **Tradier** | ⚪ **Planned** | Equities, Index Options (XSP / SPX) | `TRADIER_API_KEY` | Dedicated low-latency 0DTE route. |
| **Interactive Brokers (IBKR)** | ⚪ **Planned** | Global multi-asset, Forex, Futures | Client Portal Gateway | Safe "Draft-Only" human UI approval mode. |
| **Alpaca** | ⚪ **Planned** | US Equities, Crypto, Multi-leg Options | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Built-in paper trading sandbox. |

---

## 🎯 Feature Expansion Timeline

### Phase 1: Multi-Broker Routing (Current)
- [x] Webull OpenAPI direct execution & order preview.
- [x] Zero-loss budget risk enforcement & 0DTE position sizer.
- [x] Pre-wired Public.com client adapter (`vesper/brokers/public_broker.py`).
- [ ] Multi-account simultaneous execution.

### Phase 2: Notification & Chat Gateway
- [ ] Telegram & Discord real-time trade alert bot (sending order cards with 1-click Approve/Reject callbacks).
- [ ] Voice memo trade execution and audio thesis summaries.

### Phase 3: Advanced Portfolio Optimization
- [ ] Automated continuous delta-hedging using SPY/QQQ 0DTE options.
- [ ] Dynamic Kelly criterion scaling tied to live market regime health scores.
- [ ] Automated tax-loss harvesting and dividend capture planner.
