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

### 🚨 Phase 0: Execution Guardrails Rebuild (BLOCKING)
The pre-migration sidecar had a real order path with three properties that held
it together: **preview → confirm → place** (a single-use ticket carrying a
SHA-256 of the exact payload), **server-side guards on every path** (notional
cap, quantity cap, symbol allowlist, buying-power fraction), and a **kill
switch**. `vesper/nodes/executor.py` has none of this today — it is only inert
because `mode` defaults to `dry_run`. `vesper/risk.py`'s `RiskEnforcer` checks
per-trade risk % and a hardcoded `account_equity=10000.0` (see
`vesper/nodes/risk_gate.py`), which is position sizing, not a hard ceiling, and
isn't even reading the live account.

This phase must land — fully, with tests — **before any live-execution item in
Phase 2 or Phase 3 below ships**, and before `mode` is ever set to anything but
`dry_run` outside a test harness. See `NEXT_STEPS.md` Module 0 for the detailed
spec.

- [ ] Ticket handshake: `preview()` stages a hashed, single-use, time-limited
      ticket; `place()` takes only a `ticket_id`, never a raw order — mirrors
      the deleted `orders.py`.
- [ ] Notional cap + quantity cap, env-configurable (`VESPER_MAX_NOTIONAL`,
      `VESPER_MAX_QTY`), enforced in `risk_gate_node` against the **live**
      buying-power figure, not a hardcoded constant.
- [ ] Optional symbol allowlist (`VESPER_SYMBOL_ALLOWLIST`).
- [ ] Kill switch (`VESPER_TRADING=0`) checked at the top of `executor_node`,
      independent of `mode`.
- [ ] Wire the currently-unused `preview_order` result all the way through —
      today's "live" Webull branch in `executor.py` calls `preview_order` and
      then reports `SUBMITTED` without ever calling `place_order`; that gap
      closes only after the ticket handshake exists, not before.
- [ ] Guard the Public.com branch identically — `pub.place_order()` today is a
      direct, unguarded live call with no dry-run distinction of its own.
- [ ] `test_executor.py` / `test_risk.py` pin the handshake and caps the same
      way the old `test_orders.py` pinned rule 3, so this can't silently regress.

### Phase 2: Notification & Chat Gateway
> ⚠️ **Blocked on Phase 0.** Module 2's "Execution Callback" (tapping
> `[ Approve ]` and calling the broker directly) may not ship until the
> guardrails above exist. The alert-card / notify half of Module 2 has no such
> dependency and can proceed now.
- [ ] Telegram & Discord real-time trade alert bot (sending order cards with 1-click Approve/Reject callbacks).
- [ ] Voice memo trade execution and audio thesis summaries.

### Phase 3: Advanced Portfolio Optimization
> ⚠️ Also gated on Phase 0 — continuous delta-hedging and any automated
> position-adjusting order submit through the same `executor.py`.
- [ ] Automated continuous delta-hedging using SPY/QQQ 0DTE options.
- [ ] Dynamic Kelly criterion scaling tied to live market regime health scores.
- [ ] Automated tax-loss harvesting and dividend capture planner.

### Phase 4: Broker & Data Expansion
- [ ] `vesper/brokers/tradier_broker.py`, mirroring `public_broker.py`'s shape,
      for the dedicated low-latency XSP/SPX 0DTE route the broker matrix above
      already lists as "Planned" — gated behind Phase 0, same as every other
      live-order path.
- [ ] Wire `vesper/leveraged.py` (`get_leveraged_etfs`, `get_primary_2x`) into
      `playbooks_node` / `scanner_node` — it's a populated sqlite lookup
      (`data/leveraged_etfs.db`) that nothing currently calls. A VCP or squeeze
      candidate on an underlying should surface its leveraged proxy (e.g. NVDA
      → NVDL) as an alternate `Candidate`, sized down for equivalent risk.
- [ ] Feed the existing skills library (`skills/vcp-screener`,
      `skills/momentum-squeeze`, `skills/coil-scan`,
      `skills/institutional-flow-tracker`, the `edge-*` pipeline skills) into
      `scanner_node` as additional `Candidate` sources alongside the direct
      TraderDaddy/TickerTrace MCP calls, so Vesper reuses the screening logic
      that already exists instead of re-deriving it.
- [ ] Multi-account routing (Phase 1's last checkbox) must use `max()` across
      accounts for buying power, not `sum()` — the live Webull sidecar's own
      gotcha notes this is shared across accounts, not additive; a naive sum
      here would overstate available capital and undermine the Phase 0 cap.

### Phase 5: Feedback Loops
- [ ] Wire `reflection_node` to `mcp_server/conviction.py`'s journal: every
      executed (or dry-run-simulated) `OrderProposal` gets logged as a
      conviction and auto-resolved at 1/5/10 days, so Vesper's own historical
      hit rate becomes a scoring input for `playbooks_node` instead of living
      only in the standalone MCP tool.
- [ ] Persistent paper P&L ledger for `DRY_RUN_SIMULATED` fills (e.g.
      `data/paper_ledger.json`) that marks simulated positions to market —
      turns the current no-op dry-run mode into a running paper-trading track
      record that can justify flipping a specific playbook to live, ticker by
      ticker, instead of an all-or-nothing `VESPER_TRADING` flip.
- [ ] Dealer-gamma-aware exits in Module 3's cascade loop: replace/augment the
      flat -40%/+50% IFTTT thresholds with a crossing check against the live
      gamma flip/pin from TraderDaddy — same "compare against the *current*
      level, not a frozen number" logic the old `alerts.py` used for price
      alerts, reapplied to position exits.
- [ ] Remote kill switch: a `vesper.py halt` command (and matching `/halt` in
      the Module 2 Telegram/Discord bot) that flips the same guard `executor_node`
      checks, so the human override doesn't require an env var change and a
      service restart to take effect.
