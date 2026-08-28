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

### ✅ Phase 0: Execution Guardrails Rebuild — core landed 2026-08-28
The pre-migration sidecar had a real order path with three properties that held
it together: **preview → confirm → place** (a single-use ticket carrying a
SHA-256 of the exact payload), **server-side guards on every path** (notional
cap, quantity cap, symbol allowlist, buying-power fraction), and a **kill
switch**. All three now exist in `vesper/execution_guard.py` (`ExecutionGuard`,
`Ticket`), wired into both broker branches of `vesper/nodes/executor.py`, with
`vesper/nodes/risk_gate.py` reading live `nlv` from `wb.py` instead of a
hardcoded `10000.0`. `tests/test_execution_guard.py` pins the handshake,
caps, TTL, single-use, and payload-hash-mismatch behavior (11 tests, green).

**The kill switch (`VESPER_TRADING`) defaults OFF.** This code has not been
exercised against a live account — building it doesn't make it proven, and
going live is a deliberate action, not a side effect of a code change. Nothing
places a real order until a human sets `VESPER_TRADING=1` in the environment.

- [x] Ticket handshake: `preview()` stages a hashed, single-use, time-limited
      ticket; `place()` takes a `ticket_id` and the payload, and refuses to
      fire if the payload's hash doesn't match what was previewed.
- [x] Notional cap + quantity cap, env-configurable (`VESPER_MAX_NOTIONAL`,
      `VESPER_MAX_QUANTITY`), checked against **live** buying power
      (`wb.portfolio()["totals"]["buying_power"]`) on the Webull branch.
- [x] Optional symbol allowlist (`VESPER_SYMBOL_ALLOWLIST`).
- [x] Kill switch (`VESPER_TRADING`, default off) checked before any broker
      call, independent of `mode`.
- [x] Webull branch now actually calls `place_order` (behind the ticket) —
      the old gap where it called `preview_order` and reported `SUBMITTED`
      without ever placing anything is closed.
- [x] Public.com branch guarded identically. **Open sub-item:** `PublicBrokerClient`
      has no live buying-power lookup yet, so `VESPER_MAX_BP_FRACTION` is a
      no-op on that branch specifically — notional/quantity/allowlist/kill-switch
      still apply. Wire `pub.get_portfolio()` into a buying-power figure to close this.
- [x] `tests/test_execution_guard.py` pins the guard module directly.
      **Open sub-item:** no test yet exercises `executor_node`/`risk_gate_node`
      end-to-end with a mocked broker — the guard's unit tests don't prove the
      wiring in `executor.py` calls it correctly, they prove the guard itself
      is correct.
- [ ] **Not done, and blocking a first live trade regardless of the above:**
      exercise this against the real Webull account, one small share, with
      Webull Desktop open to watch it land — same bar the old sidecar's own
      README held itself to before its order path shipped.

### Phase 2: Notification & Chat Gateway
> Guardrails now exist (Phase 0 above), so Module 2's "Execution Callback" is
> no longer blocked on missing infrastructure — but `VESPER_TRADING` should
> stay `0` until Phase 0's last checkbox (a proven live trade) is done. Build
> against `mode=dry_run` first.
- [ ] Telegram & Discord real-time trade alert bot (sending order cards with 1-click Approve/Reject callbacks).
- [ ] Voice memo trade execution and audio thesis summaries.

### Phase 3: Advanced Portfolio Optimization
> Same note as Phase 2 — the guard layer exists, but treat `VESPER_TRADING=1`
> as a manual, deliberate step, not a default to build toward.
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
