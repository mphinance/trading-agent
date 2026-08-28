# 🚀 Vesper: Next Operational Modules Specification

This document details the architectural design and execution plans for the next operational modules of **Vesper**.

---

## ⚠️ Technical Gotchas (read before implementing any module below)

Found during a code sweep on 2026-08-28 (see `docs/CODE_SWEEP_2026-08-28.md` for
the full writeup). These are traps that look fine on a quick pass and are easy
for a fast/cheap model to get wrong — check this list before touching the
related file.

- **`executor_node`, `playbooks_node`, `analyst_node`, `regime_node`,
  `scanner_node`, `risk_gate_node`, `reflection_node` are all `async def`, but
  `wb.py`'s Webull SDK client is synchronous/blocking.** `executor_node` in
  particular calls `wb.trade.account_v2.get_account_list()` and
  `wb.trade.order_v2.preview_order()` directly inside an `async def` — that
  blocks the whole event loop for the duration of the network call, which will
  stall any other concurrent async work (a bot polling loop, an SSE stream,
  another in-flight node). Wrap blocking SDK calls in `asyncio.to_thread(...)`
  rather than calling them inline. The pre-migration sidecar hit this exact
  issue and solved it by running its alert watcher as a background *thread*,
  not an asyncio task — same fix applies here.
- **`vesper/risk.py`'s `RiskEnforcer.validate_proposal()` defaults to, and
  `vesper/nodes/risk_gate.py` always calls it with, a hardcoded
  `account_equity=10000.0`.** This is not a real cap — it's a stand-in that
  never reads the live account. Any module that assumes risk_gate is enforcing
  a real ceiling today is wrong; it isn't, until Module 0 wires it to live
  buying power.
- **`requirements.txt` is still the old sidecar's dependency list** (fastapi,
  uvicorn, `claude-agent-sdk`, comment literally says "sidecar dependencies") —
  it does **not** list `langgraph`, `pydantic`, `python-dotenv`, or
  `typing_extensions`, all of which `vesper/` imports. A clean `pip install -r
  requirements.txt` cannot run `vesper.py`. Update it before shipping any
  module that assumes a fresh install works.
- **Webull's account/order-query endpoint is rate-limited to 2 req / 2s**,
  separate from the much more generous 600 req/min market-data and
  place/replace/cancel buckets. A position-poller (Module 3) or morning
  briefing (Module 1) that calls account/order endpoints in a tight loop will
  get throttled — `wb.py`'s existing lock/backoff/stale-fallback pattern exists
  specifically for this bucket; reuse it, don't re-poll around it.
- **Buying power is shared across Webull accounts — use `max()`, not `sum()`**
  when computing available capital for multi-account routing (Phase 4 on
  `ROADMAP.md` already calls this out).
- **`sk-ant-oat…` is an OAuth token, not an API key.** Same prefix and length
  as a real key, so it passes every naive format check, but it belongs in
  `CLAUDE_CODE_OAUTH_TOKEN` — putting it in `ANTHROPIC_API_KEY` fails with
  `401 invalid x-api-key`, not an auth-token error.
- **TraderDaddy Pro's `get_conviction` takes `symbol`, not `ticker`.** Passing
  `ticker` is silently ignored (not an error) and you get the market-wide
  gauge back for every call — this will look like it's working.
- **TraderDaddy Pro doesn't declare a response charset**, so `requests`
  decodes it as ISO-8859-1 by default and em-dashes/smart quotes come back
  mangled (`â€"`). Force UTF-8 decoding on that client.
- **`--persona traderlady` (`vesper.py:36`) is parsed but never plumbed into
  session state or any node** — it's a dead flag today. Anything built on top
  of "persona switches Vesper's voice" needs to actually wire it through
  first, not assume it already works.
- **`vesper/whop.py` (the Whop licensing client) is never imported or called
  anywhere in the Python codebase**, despite being introduced in a commit
  titled "integrate ... Whop licensing engine." If a module needs license
  gating, this client still needs to actually be wired to a caller.
- **`vesper/nodes/executor.py`'s Webull branch intentionally returns
  `status="BLOCKED_PENDING_GUARDRAILS"` and does not call `place_order`** (as
  of the 2026-08-28 sweep fix — it previously claimed `SUBMITTED` after only
  previewing, which was a live-looking false positive). Don't "fix" this by
  wiring `place_order` back in without also landing Module 0's ticket
  handshake/caps/kill-switch — that would recreate the exact bug this fix
  closed, with real money behind it this time.

---

## 0. 🛡️ Module 0: Execution Guardrails Rebuild (BLOCKING — do this before Module 2 or Module 3 go live)

### Objective
Rebuild, inside `vesper/`, the three properties that made the old (deleted)
`orders.py` safe to leave running unattended: a **preview → confirm → place**
ticket handshake, **server-side caps enforced on every path**, and a **kill
switch**. `vesper/nodes/executor.py` currently has none of these — it is only
inert today because `mode` defaults to `dry_run`, and `vesper/risk.py`'s
`RiskEnforcer.validate_proposal()` checks position sizing against a **hardcoded
`account_equity=10000.0`** (see `vesper/nodes/risk_gate.py`), not a real
notional ceiling and not the live account. This is not a "nice to have" — it is
a precondition. Module 2's Approve-button execution callback and Module 3's
autonomous exit-cascade orders must not be pointed at `executor.py` until this
module ships.

### Workflow & Architecture
1. **Ticket handshake** (mirrors the deleted `orders.py`):
   - A `preview_proposal(proposal)` step runs the guards below, gets a
     broker-side cost estimate, and stages a single-use, short-TTL ticket
     keyed by a SHA-256 of the exact payload.
   - `executor_node` accepts only a `ticket_id` — never a freshly-constructed
     order — so nothing can both build and fire an order in one call.
2. **Guards, enforced server-side on every path** (Webull *and* Public.com —
   today only the Public.com branch places a real order, but both branches
   are unguarded):
   - Notional cap (`VESPER_MAX_NOTIONAL`, sane default e.g. $2500) and quantity
     cap, checked against **live buying power** pulled from `wb.py`, not the
     hardcoded `10000.0` in `risk_gate_node`.
   - Optional symbol allowlist (`VESPER_SYMBOL_ALLOWLIST`).
   - `replace`/adjust paths re-run the guards (amending can raise exposure);
     `cancel` never needs to (reducing risk is always allowed).
3. **Kill switch**: `VESPER_TRADING=0` short-circuits `executor_node` before
   any broker call, independent of `mode`. A `vesper.py halt` CLI command
   should be able to flip this without a service restart (see Module 6).
4. **Close the existing gap**: today's "live" Webull branch in `executor.py`
   calls `wb.trade.order_v2.preview_order(...)` and then immediately reports
   `status="SUBMITTED"` — it never calls `place_order`. Wiring the real
   `place_order` call only happens as part of this module, behind the ticket
   handshake, not before.
5. **Tests**: `test_executor.py` / `test_risk.py` should pin the handshake and
   the caps the same way the old `test_orders.py` pinned them, so a future
   refactor can't quietly drop a guard.

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

> ⚠️ **The alert-card / push-notification half of this module has no
> dependency and can be built now. The Execution Callback step (§3 below) may
> not be wired to a real broker call until Module 0 ships** — until then,
> `[ ✅ APPROVE & EXECUTE ]` should route to `mode=dry_run` only.

### Objective
Enable mobile trade approval and real-time alerts. Whenever Vesper generates a high-conviction trade proposal, it pushes an interactive card directly to your private phone channel with 1-click execution callbacks.

### Workflow & Architecture
1. **Bot Engine — channel-agnostic by design, not Telegram-specific:**
   - Define a small `ApprovalChannel` interface (`send_proposal_card(proposal) ->
     card_id`, `on_callback(card_id, decision)`) that the graph talks to —
     mirrors the old sidecar's `notify.Notifier` fanning out to multiple
     channels behind one interface (see `docs/CODE_SWEEP_2026-08-28.md`'s
     gotchas / the pre-migration `notify.py` pattern).
   - Ship a Telegram adapter (`python-telegram-bot`) and/or a Discord adapter
     (`discord.py`) behind that interface — pick whichever's most convenient to
     start, but the card format and the Execution Callback logic in step 3
     below must not assume a specific platform's API shape. Adding the other
     channel later, or running both at once, should mean writing one more
     adapter, not touching `executor_node` or the approval logic.
2. **Interactive Approval Card:**
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
   - Tapping **`[ Approve ]`** resolves the `human_gate_node` interrupt with
     `APPROVE` and routes into `executor_node` exactly as the interactive
     graph run does — it must go through the Module 0 ticket handshake and
     caps, not call `wb.trade.order_v2.place_order` directly from the bot. A
     1-tap mobile button is exactly the kind of low-friction path the old
     `orders.py` preview→confirm split existed to slow down; skipping that here
     because "the bot already asked" defeats the guard.
   - Tapping **`[ Reject ]`** marks the proposal as rejected and logs the rationale to the conviction memory journal.

---

## 3. 🛡️ Module 3: Active Position Monitor & 0DTE Exit Cascade Loop

> ⚠️ **Also blocked on Module 0.** This loop submits sell/stop orders
> autonomously, with no human tap in the loop at all — it inherits everything
> Module 2's Execution Callback needs and then some, since there isn't even an
> approve/reject step to catch a mis-sized order before it fires.

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

---

## 5. 🧠 Module 5: Conviction Journal Feedback Loop

### Objective
Close the loop between what Vesper actually proposed/executed and its own
track record. `mcp_server/conviction.py` already implements a standalone
directional conviction journal (log a call, auto-resolve it 1/5/10 days later,
score it) but nothing in the LangGraph pipeline writes to it — Vesper's graph
runs are currently amnesiac about their own history from one session to the
next.

### Workflow & Architecture
1. **Auto-log on proposal**: `reflection_node` writes every `OrderProposal`
   (approved, rejected, or dry-run-simulated) to the same journal
   `mcp_server/conviction.py` uses (`data/conviction_journal.json`), tagged
   with `source` (VCP / SQUEEZE / 0DTE_FLOW / WHALE_CONVERGENCE) and
   `selected_playbook` from `TradingState`.
2. **Resolve on schedule**: reuse `resolve_convictions()` as-is; it already
   fetches current price and scores against entry.
3. **Feed back into scoring**: `playbooks_node` (or a new pre-scoring step)
   pulls each playbook's resolved win-rate/hit-rate from the journal and folds
   it into `Candidate.score` — a playbook that's been wrong 4 of its last 5
   calls should rank lower today, not just get a note in a log nobody reads.
4. **Surface in Module 1**: the morning battle-plan (`vesper morning`) prints
   a one-line "last 30 days: N convictions, win rate X%" per playbook alongside
   the game-plan output, so the daily read is track-record-aware.

---

## 6. 📈 Module 6: Leveraged ETF Proxy & Skills-Library Scanner Integration

### Objective
Two pieces already exist and aren't wired into the graph: `vesper/leveraged.py`
(a populated sqlite lookup at `data/leveraged_etfs.db` mapping underlyings to
2x/3x proxies) and the 45+ skill library under `skills/` (VCP, squeeze, coil
scan, institutional flow, the edge-pipeline chain). `scanner_node` and
`playbooks_node` currently only pull candidates from direct TraderDaddy /
TickerTrace MCP calls.

### Workflow & Architecture
1. **Leveraged proxy surfacing**: when `analyst_node` produces a high-conviction
   `TechnicalAudit`/`Candidate` on an underlying that `get_leveraged_etfs()`
   has a mapping for, `playbooks_node` emits a second, smaller-sized
   `OrderProposal` on the leveraged ticker (e.g. NVDA breakout → NVDL) as an
   alternate, not a replacement — position size scaled down by the ETF's
   leverage factor so risk stays equivalent, not multiplied.
2. **Skills as candidate sources**: `scanner_node` invokes
   `skills/vcp-screener`, `skills/momentum-squeeze`, `skills/coil-scan`, and
   `skills/institutional-flow-tracker` (via the `Skill` mechanism or their
   underlying scripts) as additional `Candidate` producers, deduped against
   the existing `source` values already defined on the `Candidate` model, so
   Vesper reuses this repo's own screening work instead of re-deriving it
   against TraderDaddy alone.
3. **Autonomous skill creation, gated**: `vesper/skills_engine.py` can already
   author new skills at runtime (`create_new_skill`) with path-traversal
   validation on the slug. Longer-term: let `reflection_node` propose a new
   skill when it notices a recurring pattern in resolved convictions (Module 5)
   that no existing skill or playbook covers — draft only, human review before
   it's added to the scanner rotation.

---

## 7. 🧯 Module 7: Paper Ledger & Remote Kill Switch

### Objective
Two smaller, near-term pieces that make the `dry_run` default (Module 0) and
the eventual live flip safer and more observable.

### Workflow & Architecture
1. **Paper P&L ledger**: every `DRY_RUN_SIMULATED` fill from `executor_node`
   is appended to `data/paper_ledger.json` and marked to market on each
   `vesper morning` / position-monitor tick. Turns the current no-op dry-run
   mode into an actual running paper track record, so a playbook can be
   flipped to live ticker-by-ticker once its paper record justifies it,
   instead of an all-or-nothing `VESPER_TRADING` switch.
2. **Remote kill switch**: a `vesper.py halt` CLI command that flips the same
   flag `executor_node` checks (Module 0), plus a matching `/halt` command in
   the Module 2 Telegram/Discord bot — so the human override doesn't require
   an env var edit and a service restart to take effect once Module 2 is live.
3. **Dealer-gamma-aware exits**: in Module 3's cascade loop, augment the flat
   -40%/+50% IFTTT thresholds with a crossing check against the live gamma
   flip/pin levels from TraderDaddy (same source Module 1 already queries) —
   applying the "compare against the *current* level, not a frozen number"
   crossing logic the sidecar's old `alerts.py` used for price alerts, this
   time to position exits.
