# 🗺️ Vesper Roadmap

The single planning doc for Vesper: what's done, what's next, known gaps, and
the ideas backlog. See [`docs/TIERS_AND_FUNNEL.md`](docs/TIERS_AND_FUNNEL.md)
for the Starter (Dealer-HUD) vs. Pro (TDPro MCP + Vesper) ecosystem split.

---

## 🔌 Broker Integration Matrix

| Broker | Status | Assets Supported | Auth / Config | Notes |
|---|---|---|---|---|
| **Webull OpenAPI** | ✅ Active | Stocks, ETFs, Options, Futures, Crypto | `WEBULL_APP_KEY`, `WEBULL_APP_SECRET` | Official OpenAPI SDK, cash/margin, guarded by `ExecutionGuard`. |
| **Public.com** | 🟡 Pre-wired | Stocks, ETFs, Options, Crypto, Bonds | `PUBLIC_API_SECRET_KEY`, `PUBLIC_ACCOUNT_ID` | Guarded, but has no live buying-power lookup yet — see Known Gaps. |
| **Tradier** | ⚪ Planned | Equities, Index Options (XSP/SPX) | `TRADIER_API_KEY` | Dedicated low-latency 0DTE route. |
| **Interactive Brokers (IBKR)** | ⚪ Planned | Global multi-asset, Forex, Futures | Client Portal Gateway | See Module 4's IBKR gotchas below before building this. |
| **Alpaca** | ⚪ Planned | US Equities, Crypto, Multi-leg Options | `APCA_API_KEY_ID`/`SECRET` | Built-in paper sandbox. |

---

## ⚠️ Technical Gotchas

- **Sync Webull SDK in an async graph.** `wb.py` and `PublicBrokerClient` are
  synchronous; every blocking call inside a LangGraph node must go through
  `asyncio.to_thread(...)` or it stalls the event loop for the duration of
  the network round-trip.
- **Webull account/order-query rate limit is 2 req/2s** — separate from the
  600 req/min market-data bucket. Reuse `wb.py`'s internal cache/backoff
  rather than re-polling around it.
- **Buying power is shared across Webull accounts** — `max()`, not `sum()`.
- **TraderDaddy Pro's `get_conviction` takes `symbol`, not `ticker`** —
  wrong key is silently ignored, not an error. Force UTF-8 decoding on
  responses (no charset header, defaults to ISO-8859-1 and mangles em-dashes).
- **`VESPER_TRADING` defaults to `0` (off).** Live order execution needs it
  explicitly set to `1`. Guard config, matching `vesper/execution_guard.py`:
  `VESPER_MAX_NOTIONAL` (default $2,500), `VESPER_MAX_QUANTITY` (default 25),
  `VESPER_MAX_BP_FRACTION` (default 1.0, disabled), `VESPER_SYMBOL_ALLOWLIST`
  (optional). Ticket TTL is 120s.
- **`sk-ant-oat…` is an OAuth token, not an API key** — goes in
  `CLAUDE_CODE_OAUTH_TOKEN`, not `ANTHROPIC_API_KEY`.

---

## 🚧 Known Gaps (not yet a Module, but tracked)

**Pre-emptive fix, 2026-08-28 — `execution_guard.py`'s notional check for a
short option used the premium, not the strike.** Found while scoping the
collar-following idea below (its first real caller): selling an option to
*open* a position (a cash-secured put, a covered call) commits capital equal
to `strike * 100 * quantity` on assignment, not the few dollars of premium
in `limit_price` — the old formula (`limit_price * 100 * quantity`) would
have let a short option worth $19,000 of real risk sail past a $2,500
`VESPER_MAX_NOTIONAL` cap because the guard was looking at a $250 premium
figure instead. Nothing in this codebase constructed a SELL-to-open option
payload yet (only `monitor.py`'s exit cascade sells options, and always to
*close* an existing tracked position, where the premium-based market value
*is* the right figure) — fixed now, before the collar-following strategy
below becomes the first thing that actually needs it. Payloads must now
include `strike` for a SELL option, or `is_closing: True` if it's closing an
existing long instead of opening a new short (`monitor.py` already sets
this). Guard raises `GuardError` rather than silently under-counting if
neither is present. 4 new tests in `tests/test_execution_guard.py`.

**Start here, in this order** — cheapest/lowest-risk first, the one thing
that actually needs careful design saved for last so it doesn't get rushed:

1. **Housekeeping (done)**: `--persona traderlady` wired through `TradingState`,
   `vesper/runner.py`, and `vesper.py`; `vesper/whop.py` wired to `runner.py`,
   `vesper.py` (`--license-key`), and tested; `vesper/morning.py` fallback SPY/QQQ
   levels explicitly labeled as `STALE / UNAVAILABLE` with status indicators.
2. **Paper ledger close path (done)**: Wired `monitor.py`'s dry-run exit fills
   directly to `close_paper_position()`, updating position status to `CLOSED` and
   calculating realized PnL and account cash upon take-profit/stop-loss triggers.
3. **`PublicBrokerClient` buying-power lookup (done)**: Added `get_buying_power()`
   to `PublicBrokerClient` reading portfolio cash/purchasing power and plumbed
   into `ExecutionGuard`'s `preview()` handshake in `vesper/nodes/executor.py`.
4. **Bounce 2.0 precision — indicators were real, the entry condition wasn't
   using them. Fixed.** `mcp_server/technicals.py` genuinely computes Slow
   Stochastic(8,3,3) and RSI(2) correctly (`ta.stoch(k=8,...)`, `ta.rsi(...,
   length=2)`) and `vesper/state.py`/`analyst.py` correctly plumb them
   through — that part of the original "done" claim was accurate. What
   wasn't: `playbooks_node`'s actual `if` condition never read `rsi_2`/
   `rsi_2_prev` at all (the RSI(2) dip-then-reset trigger — arguably the
   most specific, load-bearing part of "Bounce 2.0" — simply wasn't gating
   anything), and the Action Zone and Slow Stochastic checks were both OR'd
   against a bare `rsi_14 > 45`/`<= 55`, which let almost any mildly bullish
   reading through regardless of whether price had actually pulled back —
   reintroducing the exact "trades on any bullish RSI" shape this playbook
   was rewritten to get away from in the first place. The existing test
   (`test_playbooks_node_synthesizes_bounce_2_proposal`) didn't catch this
   because its fixture didn't set `rsi_2`/`slow_k`/`keltner_lower` at all —
   it was exercising the loophole path, not the documented rules. Fixed: all
   six rules are now required (no OR-bypass), missing `rsi_2`/`slow_k` data
   means "don't draft" rather than "assume it passes," and 4 new regression
   tests in `tests/test_risk_and_bounce.py` pin the exact failure this
   caused (rejects outside the Action Zone even with bullish RSI, rejects
   without the RSI(2) dip-reset, rejects without stochastic data, rejects
   stochastic above the documented 40 threshold).
5. **Module 2's inbound ingestion layer + auth — Telegram side now done and
   started; Discord and the webhook/aiohttp option remain open.**
   `create_inbound_app()` (aiohttp) with Telegram secret-token verification,
   real Discord Ed25519 crypto verification, and REST Bearer auth is
   genuinely there and correctly wired to `approval_registry`. Found and
   fixed on an earlier review: **all three auth guards failed OPEN when
   their secret env var was unset** — `verify_*` returned `True`
   ("authorized") with no secret configured at all, meaning a deploy that
   forgot to set `TELEGRAM_WEBHOOK_SECRET`/`DISCORD_PUBLIC_KEY`/
   `VESPER_WEBHOOK_SECRET` would silently accept unauthenticated approve/
   reject/halt commands from anyone who reached the port. Now fails closed
   (rejects everything until configured), matches `deploy/install.sh`'s own
   "refuse to run unsafe, don't silently degrade" rule. Also fixed: two
   non-constant-time secret comparisons (`==` → `hmac.compare_digest`), and
   `/health`+`/approvals` both returned full pending-proposal details
   (ticker/side/quantity/price) with **no auth at all** — split into an
   unauthenticated minimal `/health` and a bearer-guarded `/approvals`.
   Added a regression test (`test_auth_guards_fail_closed_when_unconfigured`)
   and declared `aiohttp`/`cryptography` in `requirements.txt` (neither was
   listed despite being imported).
   **What's new**: `vesper/bot/telegram_polling.py` now actually feeds
   `approval_registry.handle_callback_payload()` real events, via Telegram
   `getUpdates` **long-polling** rather than the aiohttp webhook route above —
   deliberately, since a webhook needs a public HTTPS endpoint and CLAUDE.md
   rule 1 forbids binding this unauthenticated, trade-capable process to
   anything but loopback/Tailscale. `TelegramPoller` tracks the `offset`
   across calls (advancing it even for updates it doesn't act on, per
   Telegram's redelivery semantics), routes `callback_query` button taps and
   `/halt`/`/resume` text commands to `handle_callback_payload()` unchanged,
   answers the callback query to clear the button spinner, and catches
   network errors with a short backoff so one bad `getUpdates` call can't
   kill the loop. `vesper.py listen` builds the compiled graph, calls
   `approval_registry.set_graph_app()` on it (also now done at graph-build
   time in `vesper/runner.py`), and starts the loop. Tested in
   `tests/test_telegram_polling.py`: offset advancement (including past
   irrelevant updates), callback-query routing with the exact Telegram
   payload shape, missing-token no-op, and a network error that backs off
   instead of propagating. **Still open**: Discord's approval path (the
   Ed25519-verified webhook route exists in `create_inbound_app()` but
   nothing starts an aiohttp server or exposes it — that needs the bigger
   Discord rewrite noted elsewhere in this doc, out of scope for the
   Telegram work) and the generic REST webhook/`create_inbound_app()` option
   in general, which would still need a publicly reachable endpoint (or a
   tunnel) to ever receive anything — nobody currently calls it from
   `vesper.py`.
6. **LLM Reasoning & Risk Red-Teaming (done, verified safe)**: `audit_proposal_risk()`
   wired into `risk_gate_node`, but only after a proposal already passes the
   deterministic check — it can REJECT or halve `quantity` (never increase it
   or approve something the deterministic gate didn't), fails open (skips
   silently) on an LLM error rather than blocking, and is skipped entirely
   without an API key. Verified by reading the call site directly.

- **LLM reasoning: half landed.** `vesper/llm.py` (OpenRouter,
  `deepseek/deepseek-v4-flash` default) is wired into `playbooks_node` via
  `generate_candidate_thesis()` — but it only appends a narrative string to
  `audit_notes` *after* the proposal (quantity/price/side) is already fully
  constructed, so it cannot influence sizing or execution. Verified this
  directly by reading the call site. `audit_proposal_risk()` (an LLM
  red-team check on a proposal) is now wired into `risk_gate_node` — see
  Known Gaps item 6 above for what it can and can't do, and the "OpenRouter
  agent-building cookbook" section below for the one real refinement left
  (model-tier escalation, not uniform Flash for every proposal).
  `analyst_node`/`regime_node`/`scanner_node` remain pure deterministic
  Python. See "LLM layer + voice" below.
- **Callback receiver: Telegram now feeds it real events; Discord and the
  generic webhook path still don't.** `vesper/bot/inbound.py`'s
  `ApprovalRegistry` correctly uses LangGraph's `Command(resume=decision)`
  (the right mechanism — verified it doesn't call `executor_node`/a broker
  directly), and `human_gate_node` polls `approval_registry.get_decision(p.id)`
  as a fallback path. `vesper/bot/telegram_polling.py` + `vesper.py listen`
  now call both `handle_callback_payload()` (per Telegram update) and
  `set_graph_app()` (once, at graph-build time, in both `vesper.py listen`
  and `vesper/runner.py`) — see Known Gaps item 5 above for the details and
  what's tested. Tapping "Approve" on a Telegram card now resolves the
  proposal and resumes the paused graph thread. **Discord and the generic
  REST webhook path are unchanged**: `create_inbound_app()`'s aiohttp routes
  for both still exist and are still never started by anything, and Discord
  needs its own, bigger rewrite (out of scope here). Their auth story is
  also unchanged: `handle_callback_payload` itself still trusts whatever
  payload shape it's handed — the Telegram secret-token/Discord Ed25519/
  Bearer checks only apply on the aiohttp webhook routes, not on
  `telegram_polling.py`'s direct calls, which is fine for Telegram because
  the poller only relays Telegram's own `getUpdates` response (it never
  accepts inbound network input itself), but would matter immediately if a
  webhook route for Discord or a generic REST client is ever wired up.
- ~~`PublicBrokerClient` has no live buying-power lookup~~ — fixed (item 3
  above): `get_buying_power()` added and plumbed into `ExecutionGuard.preview()`.
- **No node-level integration test.** `tests/test_execution_guard.py` proves
  the guard module itself is correct; nothing exercises `executor_node`/
  `risk_gate_node`'s actual wiring against a mocked broker end-to-end.
- ~~`--persona traderlady` is parsed but never plumbed into session state~~ —
  fixed (item 1 above): wired through `TradingState.persona`, `runner.py`, `vesper.py`.
- ~~`vesper/whop.py` is never imported anywhere~~ — fixed (item 1 above):
  imported in both `vesper/runner.py` and `vesper.py` (`--license-key`).
- ~~`requirements.txt` predates the Vesper migration~~ — fixed, now has
  `langgraph`, `pydantic>=2.0`, `python-dotenv>=1.0`, `typing_extensions>=4.0`,
  `chromadb>=0.5`.
- ~~`vesper/morning.py` silently falls back to hardcoded placeholder SPY/QQQ
  levels~~ — fixed (item 1 above): fallback levels explicitly labeled
  `STALE / UNAVAILABLE` rather than presented as live.
- **Telegram/Discord webhook auth guarded the route, not the user (fixed
  2026-08-28).** `verify_telegram_webhook_secret()`/`verify_discord_signature()`/
  `verify_bearer_token()` prove a request came from Telegram/Discord's own
  servers — they say nothing about *which* Telegram/Discord user tapped a
  button. Found via the Discord gateway bot first (`ApprovalButton.callback()`
  and `on_message()`'s halt/resume had no sender check at all — anyone who
  could see the channel could approve/reject any proposal or freeze trading;
  fixed with `DISCORD_AUTHORIZED_USER_IDS`) and the identical gap existed in
  `vesper/bot/inbound.py`'s Telegram paths (`handle_callback_payload`'s
  callback-query and `/halt`/`/resume` text-command branches), which is the
  one that's actually live today via `telegram_polling.py`. Fixed with
  `TELEGRAM_AUTHORIZED_USER_IDS` (comma-separated numeric Telegram user IDs),
  same "unset -> allow with a one-time warning" default as the Discord fix
  (not fail-closed — a single-operator deployment shouldn't be locked out by
  a missing env var). Discord's legacy webhook-route interaction parser in
  `handle_callback_payload` (item 3) is now likewise guarded by `DISCORD_AUTHORIZED_USER_IDS`
  (extracting `payload["member"]["user"]["id"]` and rejecting unauthorized interactions
  with status `UNAUTHORIZED`). The generic REST webhook path (item 4/5) remains
  gated by `VESPER_WEBHOOK_SECRET` + bearer at the HTTP layer. 8 tests in
  `tests/test_inbound_bot.py`.

---

## 🎯 Modules

### ✅ Module 0 — Execution Guardrails & Live Equity (done)
- Ticket handshake: `preview()` stages a hashed, single-use, 120s ticket;
  `place()` re-hashes the given payload and refuses to fire on a mismatch.
- Server-side caps (see Gotchas above for current defaults), checked against
  **live** Webull buying power via `wb.portfolio()`.
- Kill switch (`VESPER_TRADING`, default off) checked before any broker call.
- `risk_gate_node` reads live NLV via `vesper/account.py` instead of a
  hardcoded equity constant (same fix applied in `playbooks_node`'s sizing,
  which had an independent copy of the same bug).
- 11 tests in `tests/test_execution_guard.py` and 6 node-level integration tests
  in `tests/test_execution_integration.py` (wiring `risk_gate_node` -> `executor_node`
  end-to-end for live broker submission, kill-switch / notional blocks, dry-run paper
  fills, and multi-leg synthetic combo fills).

### ✅ Module 1 — Pre-Market Battle-Plan Runner (`vesper morning`) (done)
Macro/market-health check, SPY/QQQ dealer-gamma levels, TickerTrace whale-flow
briefing, 0DTE bias, top candidates with 2x leveraged-ETF proxies. See Known
Gaps above for the stale-fallback issue.

### ✅ Module 2 — Channel-Agnostic Alert Bot & Inbound Human Gates (done)
`ApprovalChannel` interface with Telegram/Discord/webhook adapters, broadcast
from `human_gate_node`/`executor_node` — done. Inbound resolve/resume
(`ApprovalRegistry`, correct `Command(resume=...)` usage in
`vesper/bot/inbound.py`) — done for both primary bot platforms:
- **Telegram Round-Trip**: Long-polling (`vesper/bot/telegram_polling.py`) routes
  `callback_query` button taps and `/halt`/`/resume` commands into `ApprovalRegistry`
  and clears button spinners. Outbound-only, no public port required.
- **Discord Round-Trip**: Persistent gateway client (`vesper/bot/discord_gateway.py`)
  using `discord.py` and stateless `discord.ui.DynamicItem` (`ApprovalButton`) with
  regex template `r"vesper\|(?P<action>approve|reject)\|(?P<proposal_id>.+)"`. Survives
  bot restarts and view timeouts with zero in-memory state. Outbound-only WebSocket gateway.
- **Concurrent Listener**: `vesper.py listen` starts both Telegram polling and
  Discord gateway clients concurrently under `asyncio.gather()`.
- **5m chart attachment on proposal cards**: `TelegramAdapter` and `DiscordAdapter`
  call `mcp_server.charts.generate_chart(ticker, period="1d", interval="5m", show_emas=True)`
  to attach candlestick + EMA 8/21/34/55/89 charts to cards with graceful text/embed fallback.
Tested in `tests/test_bot_channel.py`, `tests/test_telegram_polling.py`, and `tests/test_discord_gateway.py`.

Found and fixed on review: **nothing checked *who* clicked Approve/Reject or
sent `/halt`/`/resume` on Discord** — any user who could see the channel/server
the gateway bot was in could approve a live trade proposal (still bounded by
`execution_guard`'s caps, but real money) or freeze/unfreeze trading, since a
Discord bot is commonly added to multi-member servers unlike a 1:1 Telegram
bot. Added `DISCORD_AUTHORIZED_USER_IDS` (optional comma-separated allowlist,
checked in `ApprovalButton.callback()` and `on_message`'s halt/resume
handler) — unrestricted by default (matches this repo's other opt-in guard
patterns) but now logs a loud warning once when unset, rather than silently
accepting anyone. 4 new regression tests
(`test_approval_button_rejects_unauthorized_user` and siblings). **Telegram's
`/halt`/`/resume` handling has the same "no sender check" gap** — it wasn't
touched here since it's a separate, pre-existing path (`vesper/bot/inbound.py`'s
`handle_callback_payload`), but it's the same class of issue and worth the
same fix.

### ✅ Module 3 — Position Monitor & Exit Cascade (done)
`vesper monitor [--interval 15] [--live] [--once]`: take-profit +50%,
stop-loss -40%, trailing breakeven +25%, 0DTE time-stop 3:00 PM ET,
dealer-gamma-flip crossing exit for SPY calls. Goes through
`execution_guard` on the live path, same as `executor_node`.

### ✅ Module 4 — Walk-Forward Backtester & Strategy Library (done)
- [x] Strategy presets: `bounce_2_pullback` (Tao of Trading 8/21 EMA pullback into Action Zone),
      `ema_crossover`, `rsi_bounce`, `macd_momentum`, `bollinger_squeeze`, `vopr_vrp_harvest`.
- [x] Metrics: win rate, profit factor, Sharpe, Sortino, max drawdown, expectancy, CAGR, tearsheet generation.
- [x] Walk-forward validation: `walk_forward_test()` with $K$-fold in-sample/out-of-sample windowing and consistency metrics.
- [x] Universe sweeping: `sweep_strategy()` across candidate stock lists. Tested in `tests/test_backtest.py`.

Spot-checked `_compute_stats` (highest-risk function — a wrong Sharpe/
drawdown formula would silently mislead every result): Sharpe/Sortino/CAGR/
max-drawdown/profit-factor are textbook-correct with proper divide-by-zero
guards. Did not review the full 1292 lines.

### ✅ Module 5 — Outcome Memory (winners, losers, and the ones we didn't take) (done)
Most of the plumbing already exists — this is fix-and-extend, not
build-from-scratch: `mcp_server/conviction.py` already has a working
log/resolve/scorecard journal (1/5/10-day resolution, WIN/LOSS/PUSH scoring),
already wired from `reflection_node`. `mcp_server/knowledge.py` already runs a
persistent ChromaDB client at `data/chromadb/` with Gemini embeddings for a
139-book knowledge RAG.

**ChromaDB decision**: keep the structured journal (JSON today, sqlite if
volume ever demands it) for win-rate filtering/aggregation — `conviction.py`'s
own docstring already made this call, and a vector DB doesn't make `GROUP BY
playbook` easier. Add a `trade_memory` collection to the **existing** Chroma
client (not a second database) for semantic "have I seen a setup like this
before" recall over free-text thesis — structured facts ride along as
metadata so a semantic hit can still be filtered by outcome.

- [x] **Phase 1 — fix the foundation.** Live `data/conviction_journal.json`
      deduplicated and protected against collisions with microsecond UUID keys.
      `reflection_node` direction mapping fixed to inspect `OrderProposal.side`
      rather than fragile message string heuristics. Added `origin`, `playbook`,
      `regime_posture`, `session_id`, `not_taken_reason`, `target_price`, `stop_loss`.
- [x] **Phase 2 — log what we didn't take.** `risk_gate_node` and `human_gate_node`
      route non-executed/rejected proposals into reflection (`origin=REJECTED_BY_RISK_GATE`,
      `origin=REJECTED_BY_USER`). Screened candidates not synthesized into proposals
      are logged with lightweight price overrides (`origin=NOT_PROPOSED`).
- [x] **Phase 3 — close the resolution loop automatically.** `resolve_convictions()`
      is automatically executed at each session reflection tick to score open
      convictions across 1d/5d/10d horizons.
- [x] **Phase 4 — semantic layer.** `ingest_trade_memory()` stores structured outcome
      vectors into ChromaDB `trade_memory` collection with robust fallback embedding;
      `recall_similar_setups()` enables semantic setup recall and outcome filtering.
- [x] **Phase 5 — feed it back into scoring.** `get_playbook_performance()` calculates
      resolved win rates and calibration adjustments, dynamically scaling proposal
      sizing and conviction in `playbooks_node`.

Verified independently: dedup is real (UUID suffix + dedup-on-load/save by
id and semantic tuple), `get_playbook_performance()` reads the exact schema
`log_conviction()` writes (checked both sides), `reflection_node` reads
direction off `prop.side` not message-sniffing, all four origins get logged
with guards against double-counting a proposal appearing in multiple state
lists, and `trade_memory` reuses `knowledge.py`'s existing Chroma client
rather than duplicating it.

### ✅ Module 6 — Leveraged ETF Proxy & Skills Library (done)
`playbooks_node` queries `data/leveraged_etfs.db` for risk-scaled 2x
alternates (fetches a **real quote** for the proxy ticker via `md.Market` —
an earlier version reused the underlying's price, which was wrong and fed a
fabricated number into the notional-cap guard; fixed). `scanner_node`
integrates `mcp_server/vcp_screener`/`screener` and TickerTrace flows.
`vesper/skills_engine.py` does path-traversal-validated autonomous skill
authoring. 5 tests in `tests/test_leveraged_and_skills.py`.

### 🟡 Module 7 — Paper Ledger (opens-only) & Remote Kill Switch (done)
- [x] `vesper halt` CLI + `vesper resume` + `/halt` bot command for instant
      remote freezing, integrated into `ExecutionGuard` and CLI telemetry.
      Reviewed: file-based, atomic write (tmp + `os.replace`), checked in
      `execution_guard._validate` before the trading-enabled check.
- [x] `record_paper_fill()` logs every `DRY_RUN_SIMULATED` fill from
      `executor.py`. Had a real bug (fixed): only debited cash on BUY/LONG,
      never credited it on SELL/SHORT — would have double-counted proceeds
      the moment a short position closed. Fixed, `tests/test_paper_ledger_and_halt.py`
      passes.
- [ ] **`close_paper_position()` and `mark_to_market()` are never called from
      any node.** `mark_to_market` is reachable manually via `vesper paper
      --mark`; `close_paper_position` is called from nowhere but its own
      test. Net effect: every paper fill opens a position that nothing ever
      closes — `vesper/monitor.py`'s exit-cascade dry-run fills don't call
      `close_paper_position`, so paper positions accumulate open forever
      instead of tracking real round-trip P&L. Needs `monitor.py`'s dry-run
      exit path wired to `close_paper_position`, matched by ticker/proposal
      to the open fill it's closing — `record_paper_fill` currently has no
      open-vs-close distinction, so a naive wire-up would open a *new*
      position instead of closing the existing one.

---

## 💡 Ideas Backlog (speculative — not committed, no phase number)

### ⭐ Do first: gaps against Michael's own documented strategy
Queried Michael's own NotebookLM strategy notes (heavily Simon Ree's *Tao of
Trading*) against the three live playbooks. Highest-signal finding of any
research pass done on this repo:

- **`momentum_squeeze` was coded backwards — now landed as "Bounce 2.0",
  partially matching Michael's rule.** Was a breakout filter
  (`ema_stack==BULLISH or rsi_14>50`); `playbooks_node` now implements a real
  pullback/mean-reversion entry (bullish EMA stack, `ADX≥18`, price within
  ±1.5 ATR of the 21 EMA as an "Action Zone" approximation, `RSI≤68` not-
  overbought filter, vol-targeted sizing). **Still doesn't match Michael's
  exact rules**: no Slow Stochastic(8,3)≤40 check, no `RSI(2)` dip-below-10-
  then-cross-back-above-10 entry trigger (uses a looser `RSI>45` momentum
  filter instead), no explicit Keltner-Channel-length-14/2x-multiplier
  calculation (uses a flat ATR band) or "close above the pullback's low
  candle" confirmation. Directionally correct now, not exact — worth a
  follow-up pass against the precise notebook rules if this needs to match
  what Michael actually trades rather than approximate it.
- **Collar-following CSP playbook (landed)**: Sells Cash-Secured Puts (CSPs)
  at the exact put strikes an option-income ETF bought as its own protective hedge.
  Configured via `VESPER_COLLAR_FOLLOW_FUNDS` (e.g. `ULTY,QQQI`, empty by default).
  Fetches full option books via TickerTrace's `get_income_fund_detail` MCP tool,
  extracts `(underlying, strike)` put pairs, fetches real-time option quotes via
  Webull market data (skips rather than fabricating if no live quote exists),
  drafts conservative 1-contract `OrderProposal` with explicit `strike` and
  strike-based assignment notional (`strike * 100 * qty`), flowing through
  `risk_gate_node` and `ExecutionGuard`'s strike-based notional caps. Tested in
  `tests/test_collar_following.py`. (Automated fund screening via
  `/api/v1/fund-effectiveness` and multi-expiry laddering remain future backlog items).
- **ADX/IV Option-Style Router playbook (landed — 3 of 4 branches)**:
  Classifies candidates by trend strength (`ADX(14) >= 20`) and IV (`IV >= 70%`):
  - `ADX < 20` + `IV < 70%` -> "Training Wheels": buy shares outright with volatility-targeted sizing.
  - `ADX < 20` + `IV >= 70%` -> "Wheel": sell Cash-Secured Put at near-the-money strike with full assignment notional (`strike * 100 * qty`).
  - `ADX >= 20` + `IV < 70%` -> "LEAPS": buy far-dated call (6-12 months out, ~180-400 DTE) with premium-based notional (`premium * 100 * qty`).
  - `ADX >= 20` + `IV >= 70%` -> "Synthetic Long": **landed** (BUY call + SELL put, same
    strike/expiry, 1:1 ratio) via the multi-leg extension below.
  Tested in `tests/test_adx_iv_router.py`.

- **Multi-leg (combo) order support — landed for Synthetic Long, general design set**:
  `OrderProposal` gained `strategy_type: Optional[str]` + `legs: Optional[List[OrderLeg]]`
  (`vesper/state.py`). `execution_guard._validate_multileg` dispatches to a **whitelist**
  of risk formulas keyed by `strategy_type`
  (`execution_guard._MULTI_LEG_RISK_FORMULAS`) — an unregistered `strategy_type` is
  refused outright (`GuardError: no registered risk formula`), same "refuse rather than
  under-count/guess" principle as the single-leg strike-vs-premium fix. This is the
  general shape any future multi-leg strategy plugs into: write a `legs -> float`
  worst-case-notional function, reason it through for that specific payoff shape, add
  it to the dict. Do **not** add a generic "sum of legs" fallback — it's wrong for
  most combos (a credit spread's max loss isn't the sum of both legs' premiums, a
  straddle's isn't either).
  - `SYNTHETIC_LONG` (only registered formula so far): treats the SELL put leg as the
    capital-at-risk driver (`strike * 100 * qty`), same reasoning as a standalone CSP —
    the long call adds unlimited upside but doesn't need its own separate cap check.
    Requires BUY-call/SELL-put, matching strike, matching expiry, matching 1:1 quantity;
    rejects anything else (a mismatched pair is a different strategy, e.g. a risk
    reversal at different strikes, that hasn't been reasoned through).
  - `vesper/nodes/playbooks.py`'s ADX/IV router Branch 4 drafts it via
    `_fetch_synthetic_long_quotes(ticker, strike)`, which fetches the option chain for
    both CALL and PUT and picks the **nearest expiry present in both** — deliberately
    not two independent `_fetch_live_option_quote` calls, which could each silently
    settle on a different nearest-dated contract and hand the guard a
    same-strike-different-expiry pair (which it would then correctly reject, but only
    after wasting an approval round-trip).
  - `vesper/paper_ledger.py`'s `record_paper_fill` now branches on `proposal.legs`:
    a combo books each leg as its own fill with its own cash impact (BUY debits, SELL
    credits), instead of the top-level `proposal.limit_price`/`side` fields — which
    only describe the primary leg — silently booking a synthetic long as if it were a
    single call purchase and dropping the short put's credit entirely.
  - `vesper/nodes/executor.py`'s `_execute_webull_multileg` calls
    `wb.trade.order_v2.place_option(account_id, new_orders, client_combo_order_id=...)`
    with each leg's live-confirmed `contract_symbol` — refuses the whole combo if any
    leg lacks one rather than guessing a contract. **UNVERIFIED against a live
    account**, same caveat as the rest of the order path: the exact leg wire-schema
    Webull expects beyond `symbol`/`side`/`quantity`/`limit_price` (its SDK's
    `add_custom_headers_from_order` reads `leg["instrument_type"]`/`leg["market"]` for
    a request header, implying a richer shape than what's sent here) has not been
    confirmed against a real combo order.
  - **Also found, not fixed**: the pre-existing single-leg `_execute_webull` calls
    `wb.trade.order_v2.place_order(payload)` with one positional dict argument, but the
    SDK's real signature is `place_order(self, account_id, new_orders,
    client_combo_order_id=None)` — two required positional args. This looks like a
    live-breaking bug, but it's inside the already-documented "not exercised against
    live" order path (see Status section), so it's flagged here rather than fixed
    blind — needs a real sandbox/paper-broker call to nail down the correct call shape
    before touching it.
  - Tested in `tests/test_execution_guard.py` (multi-leg guard section),
    `tests/test_adx_iv_router.py` (drafting + shared-expiry quote fetch), and
    `tests/test_multileg_execution.py` (paper-ledger leg-level fills, live executor
    payload shape).
  - **Thega (delta-neutral volatility harvest) remains deferred.** No concrete leg
    structure has been specified for it yet (unlike Synthetic Long, which has an
    unambiguous 2-leg definition) — per the design principle above, it stays refused
    by the guard until someone writes down its actual legs and a real risk formula for
    them. Do not add a placeholder/approximate formula just to unblock drafting.
- **Premium-recycling "free share" engine (landed — paper ledger)**:
  Sweeps cumulative options-selling realized P&L from paper ledger into accumulating
  100-share blocks of a stabilizing asset (`VESPER_PREMIUM_RECYCLE_TICKER`, default `$SGOV`),
  funded entirely from collected premium rather than fresh capital.
  - Computes `unswept_premium = realized_pnl - swept_premium`.
  - When `unswept_premium >= 100 * live_quote`, drafts a 100-share BUY proposal.
  - Premium is marked as swept only upon fill execution (`record_paper_fill`), preventing
    premature spending if rejected.
  - Tested in `tests/test_premium_recycling.py`. (Live Webull trade-history premium mining
    remains future work).
- **One strategy with zero code**: a delta-neutral "Thega" volatility harvest for high-IV binary
  events (100 shares + 1 ATM covered call + 3 ATM CSPs, net delta ≈0).
- **0DTE Flow playbook live quote fetching (landed)**: Eliminates hardcoded
  `est_premium = 1.80` placeholder. `_fetch_0dte_option_quote` filters Webull's
  option chain strictly for contracts expiring today (`datetime.now(timezone.utc).date()`),
  fetching real bid/ask/last snapshots. If no contract expires today or quote fetch
  fails, the proposal is skipped rather than fabricated. Tested in `tests/test_0dte_playbook.py`.
- **`0dte_flow` tightening (backlog)**: only run weeklies where IV>70%, sell puts at
  0.30 delta or at major OI put walls, reject wide-spread chains, harvest ATM
  CSP vega on earnings week and BTC the next day.
- **Portfolio-level circuit breaker & capital allocation buckets (landed)**:
  `vesper/circuit_breaker.py` tracks a persisted high-water-mark NLV
  (separate state file from `halt.py`'s, same atomic-write pattern) and trips
  the existing `halt()` when current NLV falls `VESPER_CIRCUIT_BREAKER_PCT`
  (default 15%) below peak. Never re-halts while already halted (no
  halt-storm, doesn't stomp the original halt reason), and starts a **fresh**
  peak the first check after a `/resume` — without that, resuming after a
  drawdown halt would immediately see the same >=15% drawdown from the stale
  peak and re-halt on the next check, making `/resume` useless for this
  specific cause. `RiskEnforcer.check_capital_allocation_buckets` (pure,
  caller supplies the position counts) enforces max 1 open long option
  position and a 20% of equity cap on `strategy_type="WHEEL_ASSIGNMENT"`
  equity holdings; both wired into `risk_gate_node`, which sources position
  counts from `paper_ledger.get_paper_positions()` in dry-run and from
  `wb.portfolio()` in live mode. **Known gap, not fabricated around**: live
  mode has no way to identify which equity shares came from a wheel
  assignment (Webull's position data carries no strategy tag), so the
  wheel-stock bucket is enforced only in dry-run/paper mode; live mode logs
  an explicit audit note that it isn't checked rather than silently
  no-op'ing without saying so, or approximating with an assumption. Closing
  this needs an actual assignment-tracking mechanism, not a guess. Also:
  nothing in this codebase currently drafts a `WHEEL_ASSIGNMENT`-tagged
  equity proposal yet (the ADX/IV router's Wheel branch only sells CSPs, it
  doesn't simulate assignment), so the wheel-stock bucket is correctly wired
  but has no real caller yet either — same shape as the strike-vs-premium
  guard fix landing before collar-following needed it. Sector-swing buckets,
  underlying-price-keyed swing-option stops, and the 25%-to-`$SGOV` tax sweep
  remain backlog (see below). Tested in `tests/test_circuit_breaker.py` and
  `tests/test_portfolio_governance.py`.
- **Backlog, not yet built**: swing-option stops keyed to the *underlying's*
  price level (200 SMA/34 EMA/lower Keltner band) instead of a fixed % on the
  contract itself; a 15% sector-concentration bucket; route 25% of high-yield
  distributions to `$SGOV` for taxes automatically (an extension of the
  premium-recycling engine's sweep logic, not a new mechanism).

### AI agent architecture
- **LLM-as-Bayesian-network-builder for explainable proposals.** An arXiv
  paper on the options wheel strategy has the LLM build a causal DAG (Market
  Regime → Strike Selection → Assignment Probability) instead of computing
  probabilities itself; a deterministic engine (`pgmpy`) populates
  conditional probability tables from historical data and does the inference.
  Their result: 15.3% annualized, Sharpe 1.08 vs 0.62 benchmark, -8.2% max
  drawdown vs -60%, ~27 recorded auditable decision factors per trade.
  Natural extension of Module 5 once there's enough resolved outcome data to
  populate real CPTs instead of priors.
- **IntellAgent-style automated evaluation** for `human_gate_node`/any future
  chat layer: build a policy graph (nodes = rules, edges = co-occurrence
  likelihood), weighted random walk to generate test scenarios at a target
  difficulty, an automated user agent converses against a symbolic mock DB, a
  separate Dialogue Critique agent audits the transcript. Worth it once
  Module 2's callback receiver exists.
- **Risk-scaled guardrail intensity**: scale `execution_guard.py`'s caps/TTL
  to context — tighter when `regime.health_score` is low, looser for
  playbooks with a proven Module-5 track record.
- Graph-structured memory (Mem0/Cognee-style) is the rung *past* Module 5's
  vector-plus-structured design, for if `trade_memory` ever needs
  relationship questions ("what's correlated with what") similarity search
  can't answer. Not now.

### OpenRouter agent-building cookbook — mapped against Vesper (2026-08-28)
Read all 6 of OpenRouter's [`building-agents`](https://openrouter.ai/docs/cookbook/building-agents)
cookbook pages and checked each against what's actually here, rather than
assuming any of them apply. One real, concrete gap; the rest are either
already covered by something that predates the cookbook or don't fit this
kind of system.

- **Advisor server tool (landed)**: `select_audit_model()` conditionally
  escalates to `PRO_MODEL` (`deepseek/deepseek-v4-pro`) for high-notional
  proposals (>= $1,000), elevated max risk (>= $250), or volatile market regimes,
  and uses `DEFAULT_MODEL` (`deepseek/deepseek-v4-flash`) for standard setups.
  Tested in `tests/test_llm_openrouter.py`.
- **Self-ask / adversarial review loop (landed)**: `audit_proposal_risk()`
  implements a single-round self-critique loop when the initial verdict is
  `REDUCE_SIZE`, `REJECT`, or flags `risk_score >= 7`, providing the model a
  reconsideration turn with the proposal's full parameters and regime context
  before finalizing the verdict (capped at 1 turn). Tested in `tests/test_llm_openrouter.py`.
- **HITL tools — already have it, and better.** The cookbook's HITL pattern
  (a tool call returns `null` to pause, a human supplies the result value)
  is TypeScript-SDK-specific and solves a narrower problem than what's
  already here. `human_gate_node`'s LangGraph `interrupt()` +
  `Command(resume=...)` — verified correct in `vesper/bot/inbound.py` — is
  the framework-native version of the same idea. Nothing to add.
- **Create headless agent — already have it.** `vesper.py`'s CLI
  (`scan`/`morning`/`monitor`/`halt`/`paper`, cron-invokable) already is this
  pattern in Python instead of the TypeScript SDK the cookbook targets. One
  portable idea worth an explicit test rather than assuming it's covered:
  the doc's rule that retries must only happen *before* a tool's mutating
  side effect fires, never after. `execution_guard.py`'s single-use ticket
  (`used=True` on redeem) almost certainly already prevents a double-fire on
  retry — worth a test that pins this rather than trusting it by
  inspection, the same way `test_execution_guard.py` already pins the rest
  of the ticket handshake.
- **Subagent server tool — not yet applicable.** Delegating routine subtasks
  to a cheaper worker model only makes sense from inside multi-step
  orchestration with parallelizable subtasks. `playbooks_node`'s one LLM
  call is single-shot, not a loop. Would become relevant if per-ticker
  thesis generation, or Module 1's morning briefing, ever grows to fan
  research out across multiple tickers or sources — not now.
- **Create agent harness (TUI) — not applicable.** This is a codegen
  scaffold for building a brand-new TypeScript CLI product from scratch;
  the page itself says "if you're already using Claude Code... you probably
  don't need this." Vesper already has a CLI and bot channels in Python —
  wrong stack for an already-existing product, not a gap to fill.

### Trading-specific
- ~~Vol-targeting position sizing~~ — already landed as
  `RiskEnforcer.calculate_vol_targeted_size` (`vol_scalar = clip(daily_target_vol
  / realized_daily_vol, 0.4, 1.6)` scaling `effective_risk_pct`), used by the
  ADX/IV router's Training Wheels branch and Bounce 2.0.
- **Holly (Trade Ideas AI)'s nightly re-optimization**: re-backtest strategy
  variants nightly, only activate ones clearing a win-rate/reward:risk bar
  for the next session. Ties Module 4 and Module 5 into an actual gate on
  what `scanner_node` runs tomorrow, not just a score after the fact. Also
  worth stealing: risk-segmented playbook "personas" (maps onto the existing
  `--playbook` flag) and continuous streaming over interval polling.
- **Hedge-vs-directional options flow classification (landed & wired into scanner)**:
  `vesper/flow_classifier.py` implements pure deterministic scoring (`DIRECTIONAL`,
  `HEDGE`, `AMBIGUOUS`). Confirmed TraderDaddy field schemas:
  - `get_unusual_activity`: `volume` (int), `openInterest` (int), `vsOI` (float %), `type` ("CALL"|"PUT"), `sentiment` ("Bullish"|"Bearish"), `moneynessPct` (float), `score` (int).
  - `get_iv_rank`: `ivRank` (0-100 float), `atmIv` (float).
  - `get_gex_ticker` / `td.levels()`: `gammaFlipLevel` (float), `spotPrice` (float).
  Classifies large size vs OI far from gamma flip with high IV rank as `DIRECTIONAL`,
  and size clustered at gamma flip (|dist| <= 0.75%) with low/moderate IV or ATM put overlay
  as `HEDGE`. Wired directly into `vesper/nodes/scanner.py` to promote directional flow
  to `Candidate` (source `UNUSUAL_FLOW`) and filter out dealer hedges. Tested in
  `tests/test_flow_classifier.py` and `tests/test_scanner_flow.py`.
- **Vanna/Charm exposure (VEX/CHEX)**: Checked 2026-08-28. Confirmed TraderDaddy Pro
  does NOT expose Vanna or Charm exposure (checked all 30 TDPro tool schemas and live
  response payloads for `get_gex_ticker`, `get_gex_overview`, `get_edge_xray`, and
  `get_hedge_analysis`). If required in the future, VEX/CHEX would need in-process
  Greek computation (e.g. via `scipy` / `py_vollib`) over Webull option chains rather
  than an upstream TDPro endpoint.
- Tooling worth knowing about: `vectorbt` (Module 4 candidate), `quantstats`
  (Sharpe/Sortino/drawdown tearsheets + Monte Carlo bust-probability sim for
  the approval card), `py_vollib` (fast Black-Scholes IV/Greeks if options
  math needs to move in-process).

### IBKR integration gotchas (for whenever `ibkr_broker.py` gets built)
- **One login session per account, globally** — TWS/Gateway/Client Portal/
  mobile all fight over one slot. Create a **separate IBKR user** for the bot
  (no withdrawal rights, IP-restricted); never share credentials with the
  account you check manually.
- **Weekly forced session reset** (Sat night/Sun morning). Paper accounts can
  auto-restart cleanly (`ColdRestartTime`); **live accounts need a human to
  ack 2FA on the IBKey app at least once a week** — can't be fully unattended.
- Configure Gateway for **auto-restart, not auto-logoff**, or 2FA is needed
  daily instead of weekly.
- **Nightly 30s-5min blackout**: socket stays connected, no data flows, order
  placement fails silently. Any held position needs an exchange-side
  bracket/stop for that window.
- **`CLOSE_WAIT` socket rot**: a dropped connection that isn't cleanly closed
  leaves the process looking alive (PID present) while doing nothing — needs
  an external watchdog on `ss -tnp`, not just a process-alive check.
- **No open/close semantics** — only `BUY`/`SELL`. A SELL larger than the
  current long silently becomes a short; the risk layer must track net
  position itself.
- **CBOE's "390 rule"**: average >1 option order/minute across a month and
  the account gets reclassified "professional" (worse fees, worse execution
  priority) — a rate cap belongs in the guard layer before this ever goes live.
- Pacing: max 60 historical-data requests/10min (Error 162 if exceeded), 100
  concurrent streaming quotes by default, Docker/WSL2 needs Gateway's
  loopback-only restriction lifted or a `socat` relay.

### MCP servers worth connecting
Named, non-redundant against what's already connected (webull, momentum,
tickertrace, traderdaddy, tradingview, context7):
- **SEC EDGAR** / **ShareSeer** — 10-K/10-Q text mining and Form 4 insider
  data; cross-references against TickerTrace's institutional flow.
- **Polymarket** — prediction-market pricing as a real-time probability
  matrix for macro events (Fed decisions, FDA approvals).
- **LunarCrush** — social sentiment/hype metrics, if a sentiment-analysis
  pipeline ever gets built (aspect-based, multi-entity scoring — not
  document-level — is the credible approach per the sources reviewed).
- **QuantConnect** — cloud-scale backtesting engine, candidate
  alternative/complement to `vectorbt` for Module 4.
- Lower priority for a solo system: Alpaca (redundant broker), FIXParser
  (institutional order routing), dune-analytics-mcp/Twelve Data (only
  relevant if this goes multi-asset).

### Not yet researched
A few more of Michael's NotebookLM notebooks looked relevant but weren't
queried: *TraderDaddy Pro Docs & How-Tos* (might surface unused endpoints —
worth checking since TraderDaddy is already a core dependency), *Modern
API-First Brokerage and Algorithmic Trading Systems*, *The Only Trading
Library You'll Ever Need*, *The End of the Hedge: Global Macro and Regime
Shifts*, *Global Financial Markets: Volatility, Derivatives, and Risk*.

### 🗣️ LLM layer + voice

**Model/text half: landed.** `vesper/llm.py` + `deepseek/deepseek-v4-flash`
via OpenRouter, wired into `playbooks_node` for thesis narratives. Setup and
usage now live in `docs/OPENROUTER_PRICING_GUIDE.md` rather than here — this
section is what's still *not* built.

**Still open**: `audit_proposal_risk()` exists in `vesper/llm.py` but is
called from nowhere — an LLM red-team check on a proposal, currently dead
code. Whether it's worth wiring in (as an advisory signal alongside, never
instead of, `execution_guard`'s deterministic checks) or left unused is an
open call. Whether `playbooks_node`'s thesis-only integration is the right
ceiling, or whether an LLM should ever influence sizing/entry logic itself
(vs. today's narrative-only, zero-influence role) — the Bayesian-network
pattern in "AI agent architecture" below is the more rigorous version of
that question, and better thought through before an LLM gets any real
influence over a proposal's numbers, if that's ever wanted.

**Voice: not built yet, but the decision is made — standalone, own voice
stack, DeepSeek stays the brain.** (2026-08-28) Considered wiring Vesper's
voice interface into `nyx` (`mphinance/nyx` on host `coolify`, née a fork of
`six-ddc/disclaw`) instead of building Vesper's own — nyx already has a full,
running voice pipeline (Discord voice channel, wake-word, Whisper STT +
Kokoro TTS both via OpenRouter, hub routing into a Claude Agent SDK brain)
that would have been a small integration (one more key in its
`createMcpServers` factory). **Decision: keep Vesper standalone.** Reasons
that mattered most:
- **Blast radius mismatch.** nyx also runs the TD Pro agent launcher, Sleeper
  fantasy cards, and other business-facing automation — a live trading
  assistant's uptime/security would depend on a system that also runs
  fantasy football.
- **Shared, budgeted Claude runner.** nyx's agent-run poller caps concurrency
  and daily runs specifically because it's shared across every other nyx
  thread — a trading voice query would queue behind unrelated business-agent
  runs.
- **Widens the exact surface this whole roadmap has been careful about.**
  Putting Vesper's tools behind a general-purpose assistant that also
  handles business ops means a stray utterance in the wrong channel is
  adjacent to trading tools instead of isolated from them — same class of
  risk as the inbound-approval-auth gap already tracked above, just
  relocated rather than solved.
- Not merged to `main` on the nyx side either — building on it would stack
  two moving targets.

**What's still worth reusing from nyx as validated reference, not as a
dependency**: the exact model pair — STT `openai/whisper-large-v3-turbo`,
TTS `hexgrad/kokoro-82m`, both callable via OpenRouter, both already proven
in production for trading-adjacent speech — and the vocabulary-biasing
trick (`voice/config.ts`'s `STT_VOCAB`): feeding Whisper an initial-prompt
hint of tickers/options jargon (their example: without it, "Nyx" transcribed
as "Nix" — the same class of mistake CLAUDE.md's old sidecar notes already
warned about for NVDA→"in video"). Same fix, cheaper than a bigger model,
worth carrying into Vesper's own STT config verbatim.

Also still relevant from the earlier `princezuda/safestclaw` /
`moltis-org/moltis` research: both validate the same overall shape (one
process, MCP client to existing tool servers, swappable model provider,
multi-channel) that `vesper/bot/` should be built as an extension of, not
adopted wholesale as external frameworks — same reasoning as the nyx
decision above, one level down.

**Decided: both sides get logged as text, always.** Whatever channel voice
ends up on, the transcribed input *and* the spoken-reply text both get
written to a durable text record — not just spoken and gone. This isn't
optional/configurable: audio is ephemeral and unauditable, and a system that
can act on spoken trading commands needs the same auditability as everything
else here (the `audit_trail` entries every node already writes, the
conviction journal, `execution_guard`'s ticket digests). Natural home is
alongside those — e.g. an entry in the existing audit trail or a dedicated
voice-transcript log, not a new parallel logging scheme. Whatever posts the
Discord/Telegram message already *is* that text log if voice-in posts its
transcript as a visible message before acting on it (nyx's own
`onVoiceUtterance` does exactly this — posts `🎙️ <@user>: {text}` to the
thread before submitting it) — worth copying that pattern rather than
inventing a separate one.

**Decided: the wake word is "Vesper."** (2026-08-28) Two syllables, unisex,
and — per nyx's own `STT_VOCAB` lesson (its "Nyx" → "Nix" mishearing without
an explicit bias hint) — worth seeding Vesper's own STT vocab hint with
"Vesper" from day one rather than discovering the same mishearing the hard
way. Note this holds even if the product itself ends up branded "Vespryx"
(see `docs/TIERS_AND_FUNNEL.md` / the nyx `thelist.md` business notes) —
the spoken wake word and the product name don't have to match, the same way
a product can be called one thing and answer to a shorter spoken name.

**Not decided yet, deliberately**: whether voice-in comes from a Discord
voice message, a Telegram voice note, or something else; whether TTS output
posts as a voice-note reply or stays text-only with voice reserved for
alerts; and whether this belongs in `vesper/bot/` as extensions to the
existing adapters or as a new sibling module. Flagging the shape, not
committing to the implementation.
