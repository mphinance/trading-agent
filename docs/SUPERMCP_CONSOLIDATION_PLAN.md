# supermcp consolidation plan — folding Vesper into the live hub (2026-09-01)

**Status: PROPOSAL. No code has been written. Nothing here should be implemented
without Michael's explicit sign-off** — see [Open decisions](#open-decisions) at the
end. This touches a live brokerage account with real money and reverses several
deliberate, documented safety decisions.

## The target shape

One system instead of three:

| Layer | Today | After |
|---|---|---|
| Voice | Nyx (disclaw), no trading access at all | Nyx (disclaw), jarvis hub — the interface |
| Data + execution | split across supermcp (live, thin) and Vesper (guarded, local) | supermcp — the authenticated hub |
| Safety logic | Vesper's `execution_guard` / `risk` / `circuit_breaker` | ported into supermcp, applied to every order path |
| Approval | Telegram/Discord inline buttons | spoken ticket-ID confirm-back |
| Orchestration | Vesper's LangGraph pipeline | retired — the logic survives, the wrapper doesn't |

## What the investigation actually found

Three findings changed the shape of this plan versus how it was scoped. All three are
**pre-existing conditions**, not risks introduced by consolidating.

### F1 — supermcp's live order path is weaker than Vesper's in every dimension

Prod (`vultr`, `/home/mphinance/supermcp`) runs with `WEBULL_ENVIRONMENT=prod`,
`LIVE_ORDERS_ENABLED=1`, and critically `UNGATED_ORDER_BROKERS=webull`. That last
setting means `orders.execute()` **waives both** the `LIVE_ORDERS_ENABLED` kill switch
and the `confirm="SEND IT LIVE"` phrase for Webull specifically. A Webull order arms on
*affordability + quantity > 0* alone.

| Control | Vesper `execution_guard.py` | supermcp today |
|---|---|---|
| Notional cap | $2,500 (`VESPER_MAX_NOTIONAL`) | $10,000 (`WEBULL_MAX_ORDER_NOTIONAL_USD`) |
| Quantity cap | 25 (`VESPER_MAX_QUANTITY`) | 1,000 (`WEBULL_MAX_ORDER_QUANTITY`) |
| Kill switch | `VESPER_TRADING`, **defaults off** | `LIVE_ORDERS_ENABLED` — **waived for Webull** |
| Confirm phrase | ticket handshake | `"SEND IT LIVE"` — **waived for Webull** |
| Emergency halt | `vesper/halt.py`, checked first | none |
| Circuit breaker | 15% trailing-peak NLV auto-halt | none |
| Buying-power fraction | `VESPER_MAX_BP_FRACTION` | none |
| Payload integrity | SHA-256 ticket, 120s, single-use | none |
| Idempotency | ticket is single-use | none — a retry places a second order |
| Options | supported, strike-sized | **hard-refused** |

The 4× notional gap and the two waived gates are the headline. This is not a green
field to port guards into; it is a second, thinner, already-armed order path.

### F2 — the dual-client hazard is worse than "two clients"

Both codebases drive `webull-openapi-python-sdk` against the same app key on the same
live account, on **different pins** (Vesper `2.0.16`, supermcp `2.0.12`).

Vesper's `wb.py` has a lock, backoff, and stale-fallback specifically for Webull's
scarce **2 req / 2s** order-query bucket. supermcp's `brokers/webull.py` has **none** —
every call is a bare `asyncio.to_thread`, and *every failure is swallowed into an
empty/inert result*. So a rate-limit collision doesn't raise on supermcp's side; it
silently returns empty positions or empty balances. An empty position list makes a
closing order look like an opening one, and an empty balance makes affordability
checks meaningless. Those two processes are on different machines, so no in-process
lock can coordinate them.

### F3 — the jarvis hub has no MCP wiring, and is *less* fenced than the shared war-room

Definitively answered: the `#supermcp` channel's Claude session gets
`cwd=/home/mph/supermcp` with **full Bash/Read/Write/Edit/WebFetch** and **no MCP
connector to `mcp.mphinance.com`**. Verified — no `.mcp.json` at `/home/mph/` or in the
supermcp clone, and `~/.claude.json`'s `projects["/home/mph/supermcp"].mcpServers` is
empty. The only MCP server disclaw ever registers is its own fixed Discord+cron set
(`src/mcp-server.ts:91`), which is channel-agnostic by construction.

Consequences:

- That session can `curl` supermcp's live API or run its Python directly. Any tool-level
  guard is advisory, not enforced.
- `DISCLAW_PERMISSION_MODE=bypassPermissions` — no approval prompts.
- **The fencing is inverted from what was assumed.** `src/ambient.ts:83-96` denylists
  `Bash, BashOutput, KillShell, WebFetch, WebSearch, Task` for the *shared, anyone-can-post*
  war-room hub. The *private* jarvis hub carries no such denylist. The compensating
  control exists only on the hub that was believed to be the dangerous one.
- **Voice cannot reach trading at all today** — `onVoiceUtterance` (`src/hub.ts:175`)
  routes to the voice hub with `cwd=/home/mph/warroom` and only disclaw's tools. The
  voice→trade path is entirely net-new, not a rewiring.
- disclaw has **only a denylist** mechanism. There is no positive "permit only these
  tools" primitive to build on.

---

# Phase 0 — Containment

**Independent of consolidation. Worth doing even if the rest is never built.**
Nothing here is a new feature; it closes gaps that are open right now.

1. **Close the ungated Webull path.** Remove `webull` from `UNGATED_ORDER_BROKERS` so
   `LIVE_ORDERS_ENABLED` and the confirm phrase apply again, *or* consciously decide
   the adapter caps are the intended sole control and lower them to Vesper's numbers.
   Doing neither leaves a $10k/1000-share ungated path live.
2. **Align the caps** to $2,500 / 25 until a deliberate decision raises them.
3. **Fence the jarvis hub.** Either add `extraDisallowedTools` for `Bash` on trading
   channels (mirroring what `ambient.ts` already does), or accept raw shell there as a
   conscious choice. Today it is neither — it is an unexamined default.
4. **Pin the worker count explicitly.** systemd runs `uvicorn src.app:app` with no
   `--workers`, so it is 1 by default. Every design below (ticket store, monitor thread,
   the existing warmer thread) silently breaks or *duplicates* if that ever changes.
   Make it explicit and comment why.
5. **Reconcile the SDK pin** (2.0.12 vs 2.0.16) before any shared-client work.

**Doc update on landing:** supermcp's own CLAUDE.md; a note in this repo's ROADMAP.

---

# Phase 1 — One Webull client owner

Resolves **F2**. This is the most urgent item and does not depend on anything else.

**Recommendation: supermcp becomes the sole owner of the Webull SDK client.** It is
always-on, authenticated, and is where execution is heading anyway. Vesper's live
Webull access is retired progressively (Phase 8), not in one cut.

Steps:

1. **Port `wb.py`'s rate-limit discipline into `brokers/webull.py`** — the lock, the
   backoff, and the stale-fallback for the 2 req/2s order-query bucket. This is the
   single highest-value port in the whole plan and is independent of guards.
2. **Stop swallowing failures into empty results.** An exception must be distinguishable
   from "genuinely zero positions." Returning `[]` on a rate-limit error is how a
   closing order becomes an opening one.
3. **Interim coordination while both still run:** put `vesper loop` in `dry_run`/paper,
   or run the two on non-overlapping schedules. Two machines cannot share a lock; only
   *not running both against the live bucket* actually resolves it.
4. Preserve the `wb.py` / `md.py` split rationale — the scarce order-query bucket and the
   generous 600/min market-data bucket are different clients on purpose. Do not merge
   them on the supermcp side either.

---

# Phase 2 — Port the order guard and the ticket handshake

Resolves **F1**. Replaces `webull.py`'s `_guard()` and extends `orders.py`.

`vesper/execution_guard.py` is **already broker-agnostic** — it takes plain dicts (not
Pydantic models) and the broker call is an injected `place_fn` closure. Its only coupling
is a lazy `from vesper.halt import is_halted`. It lifts nearly verbatim into a new
`src/guard.py`.

What must port:

- **Strike-based sizing for SELL-to-open options.** A short option commits
  `strike × 100 × qty` on assignment, not the premium. Reading `limit_price` let a
  $19k risk past a $2.5k cap — this was a real caught bug. Payloads must carry `strike`
  for a SELL option or `is_closing: True`; the guard raises rather than under-counting
  when neither is present. **supermcp hard-refuses options today, so this must land
  before options are ever wired** — otherwise the bug gets reintroduced from scratch.
- **The multi-leg formula whitelist.** Exactly two registered entries today
  (`SYNTHETIC_LONG`, `THEGA`) with fixed leg-ratio validation. An unregistered
  `strategy_type` is refused outright, never approximated.
- **The ticket handshake.** `preview()` runs guards and stages a SHA-256 digest of the
  exact payload; `place(ticket_id, payload, place_fn)` re-hashes and refuses a mismatch.
  Single-use, 120s TTL. This also gives supermcp the **idempotency key it currently
  lacks**.
- **The kill switch and halt check**, defaulting off.
- Config read **fresh per call**, not cached at import — supermcp's `config.py` is
  import-time constants, so this is a deliberate deviation from local convention.

**The one defect that must be fixed in transit, not carried:**
`ExecutionGuard._tickets` is an in-memory dict whose docstring scopes it to "one
process's worth of orders." Under uvicorn that is a latent multi-worker bug, and it is
the only safety-critical state with no persistence. This repo **already hit and fixed
this exact bug class** on 2026-08-29, when the LangGraph checkpointer and approval
registry were RAM-only and would strand a pending approval across a restart. Make the
ticket store disk-backed JSON following supermcp's existing `data/<name>.json`
convention, or pin one worker and document why.

**Tests port too.** `tests/test_execution_guard.py` (31 cases) pins the handshake, the
caps and the strike-vs-premium rule; `test_multileg_execution.py` (9) and
`test_execution_integration.py` (8) cover the rest. supermcp has no equivalent suite —
these should arrive with the code, not after.

---

# Phase 3 — Portfolio-level risk

Vesper's per-order caps and its portfolio risk are **different concerns**: a series of
individually-compliant orders can still bleed an account dry. supermcp has *nothing* at
this level.

**Where it should live: a new `src/portfolio_risk.py`, called from `orders.py`'s
execute path — not folded into `brokers/webull.py`.** The whole point is that it applies
regardless of which tool or which broker originates an order. Putting it in the adapter
would scope it to one broker and invite a second copy later.

Ports:

- `vesper/risk.py` — capital-allocation buckets, sector concentration (15%), vol-targeted
  sizing. Pulls in `vesper/sector.py` (yfinance ticker→sector, with its cache).
- `vesper/circuit_breaker.py` — trailing-peak NLV, 15% default drawdown → auto-halt.
- `vesper/halt.py` — the freeze file, checked before anything else.
- `vesper/audit_chain.py` — hash-chained audit log.

Keep `halt_state.json` and `circuit_breaker_state.json` as **separate files**, as they
are today. "Are we halted and why" and "what peak are we measuring from" are different
concerns; conflating them makes halt's boolean model do double duty.

**Migration gotcha:** the persisted `peak_nlv` is currently `100000.0` — the *paper*
account's seed, not the live NLV. Carrying that value across would leave the drawdown
halt either permanently un-trippable or instantly tripped. Re-seed against the live
account on first run, deliberately.

**Dependency note:** `sector.py` needs `yfinance`, which is **not installed** on the
supermcp venv (no scipy, no py_vollib, no yfinance today; numpy/pandas are present only
as transitive deps).

Tests: `test_sector_concentration.py` (17), `test_portfolio_governance.py` (19),
`test_circuit_breaker.py` (8).

---

# Phase 4 — Jarvis hub tool wiring (read path only)

Resolves **F3**, and deliberately stops short of the write path.

This follows Michael's own recorded build order for voice: *"voice-in → STT → read-only
query → text reply first, so STT and routing are provable without TTS in the loop."*

1. **Register supermcp as an MCP server** for the trading channel(s) in disclaw's
   `createMcpServers` factory — the mechanism exists and is a small change; it has simply
   never been used for an external service.
2. **Denylist `Bash`/`WebFetch` in that session.** Without this, the MCP tools are
   decoration — the session can still `curl` the API directly. disclaw has no positive
   allowlist, so a denylist is the only available primitive.
3. **Reconsider `bypassPermissions`** for a session that can now reach an order path.
4. Prove read-only voice end to end: P&L, positions, the gamma flip, what's open.

Nothing in this phase can place an order. That is the point — it makes STT, routing and
tool wiring provable while the blast radius is still zero.

---

# Phase 5 — The spoken ticket-confirm ⚠️ REQUIRES SIGN-OFF

**This is the phase that reverses documented safety decisions.** It should not be built
on the strength of this document alone.

What it reverses:

- **Rule 4d** — "voice asks questions; buttons move money." The reasoning on record: a
  transcript is ambiguous in exactly the wrong place ("approve" — *which* proposal?),
  whereas buttons are unambiguous, per-user authorised and restart-safe.
- **Rule 3's approval model** — Telegram/Discord inline buttons as the human gate.
- **The 2026-08-28 nyx decision** (ROADMAP) which explicitly rejected wiring Vesper's
  voice into disclaw, for four named reasons. Two are still live and need answers:
  *blast-radius mismatch* (disclaw also runs the TD Pro agent launcher, Sleeper fantasy
  cards and business automation) and the *shared, budgeted Claude runner* (a trading
  query queueing behind unrelated agent runs).

**The proposed replacement gate.** A ticket-ID-scoped spoken confirm-back:

> "Buy 100 NVDA at market, ticket 4F2A — say 'confirm 4F2A' to send it."

This preserves the property that actually matters — *a machine-checkable binding between
the approval and one specific payload* — while removing the app-switch Michael doesn't
want. Silent server-side caps from Phase 2 still apply on every placement attempt
regardless of who confirmed it, so the voice gate is a *second* control, never the only one.

Design points that are genuinely new work, not reuse:

- **Ticket IDs are `uuid4().hex` today — 32 hex chars, unspeakable.** A voice code needs
  to be short, phonetically distinct, and collision-checked, avoiding the B/D/E/P/V/3
  confusion set. This is a new ID scheme alongside the existing one.
- **The 120s TTL was sized for a machine-speed preview→place inside one execution pass.**
  A human hearing a proposal, thinking, and speaking back will routinely exceed it.
  Extending it is the obvious fix and directly weakens the freshness guarantee the TTL
  exists for — a limit price approved five minutes ago may no longer be sane.
  Needs a decision, ideally a shorter TTL with cheap re-preview rather than a long one.
- **Ticker mis-transcription** (NVDA → "in video") is mitigated by reading the resolved
  symbol back in the confirm-back — the operator hears what the machine actually parsed
  before confirming. disclaw's `STT_VOCAB` and `DEFAULT_ALIASES` already solve much of
  this and should be extended, not reinvented.
- **Both sides logged as text, always** — per the standing decision. Post the transcript
  as a visible message *before* acting, so a mishearing is visible while it still matters.
- **No per-caller scoping exists on MCP tools today.** Owner-vs-subscriber checks live
  only in HTTP route handlers (`accounts.is_owner`); none of the 37 `@mcp.tool` functions
  take a request context. An owner-only *tool* mechanism has to be built.

---

# Phase 6 — Continuous monitoring and alerts

The 0DTE **-40% stop**, the exit cascade, and the dealer-gamma alert watcher are
always-on processes, not request/response tools. Mapping them onto a FastMCP server is
the genuinely awkward part of this plan.

**Good news: the pattern already exists on the supermcp side.** `app.py`'s
`_start_warmer()` runs a daemon `threading.Thread` with its own event loop, started at
import, looping every `WARM_INTERVAL_MIN`. That is proof a background worker can live
inside the uvicorn process with no new infrastructure — and it mirrors Vesper's own
reason for making the alert watcher a *thread* rather than an asyncio task (the Webull
SDK is blocking; a slow snapshot inside the event loop would stall everything).

**Recommendation: follow the warmer pattern, with one hard change.** The warmer swallows
every exception, which is fine for a cache. **It is not acceptable for a stop-loss.** A
silently-dead monitor is strictly worse than no monitor, because the operator believes a
stop is in place. Requires: a heartbeat file, a staleness check, and a loud Discord alert
when the loop stops advancing.

**On disclaw's cron as an alternative:** it is real and persistent (`cron_jobs` SQLite
table, reloads on boot, Discord button panel). It is a good fit for *scheduled scans*.
It is a **bad fit for stops**, because it auto-pauses a job after 3 consecutive failures —
for a scan that is sensible backpressure; for a stop-loss it is a silent disarm. Use it
for scans, not for the monitor.

Also to resolve:

- **Multi-worker duplication.** Two workers means two monitor threads means duplicate
  exit orders. Same root cause as the ticket store (Phase 0, item 4).
- **`stream.py`'s gRPC push feed** wakes the monitor in ~1s instead of a 15s poll, which
  matters a lot for a -40% 0DTE stop. Whether it can run from vultr is unverified. It
  must degrade to polling, never break — that property is already tested
  (`test_stream_runner.py`, 8 cases) and the test should travel.
- **Alert watcher** brings `alerts.py` + `quotes.py` + `notify.py`. Both crossing
  properties are load-bearing and covered by `test_alerts.py` (23 cases): never test
  `price <= level`, and a moving level must never fire on its own. `resolve_level()`
  returning `None` rather than a remembered number on a TDPro outage is deliberate.
- **The ntfy topic is a credential, not a name.** It must never appear in a `status()`
  response. Pinned by `test_notify.py`.

---

# Phase 7 — Port the playbooks

The largest lift, and deliberately last: it depends on Phases 1–3 being in place, and
it is the one phase that is purely additive value rather than risk reduction.

**The structural finding that reframes this phase:** the playbooks **do not compute their
own technicals.** `playbooks_node` only *reads* `state["technicals"]` and
`state["options_audits"]`, populated upstream by `analyst_node` from
`mcp_server/technicals.py` (pandas_ta over yfinance) and `mcp_server/options.py`
(hand-rolled Black-Scholes in `options_greeks.py`). Note there are **two independent
Black-Scholes implementations**: py_vollib in the 0DTE strike selector only, scipy
elsewhere.

So there are two viable designs, and this needs a decision:

- **(a) Injected audits** — ported tools take pre-computed technicals/options audits as
  parameters. Smallest port, but pushes the work onto the caller and means the remote
  tools aren't self-sufficient.
- **(b) Port the analyst layer too** — vendor `technicals.py`, `options.py`,
  `options_greeks.py` and their yfinance dependency into supermcp. Self-sufficient, but
  meaningfully grows a deliberately-thin dependency tree.

**Note this does *not* require supermcp to reach back to the local box.** The
`mcp_server/` code is a plain importable Python package, not accessed over MCP — Vesper
imports it in-process. So the choice is vendoring versus parameter-passing, not a network
callback. That hazard, flagged as a risk in scoping, does not exist.

Per-playbook portability:

| # | Playbook | Port difficulty | Blocker |
|---|---|---|---|
| 1 | 0DTE Flow | easy | needs `py_vollib` (absent on supermcp) |
| 2 | Tao Bounce / Momentum Pullback | medium | needs technicals; 2x-proxy needs `data/leveraged_etfs.db` (tracked in git — travels free) |
| 3 | Collar-Following | easy | TickerTrace is a plain outbound REST call |
| 4 | ADX/IV Router | easy | needs technicals + options audits |
| 5 | THEGA | easy | multi-leg — needs Phase 2's formula whitelist |
| 6 | Premium-Recycling | **blocked** | reads realized premium from local `data/paper_ledger.json` |
| 7 | Tax Reserve Sweep | **blocked** | same local ledger dependency |
| 8 | Earnings-Week CSP | easy | TDPro `get_earnings_flow` is market-wide; filtering is client-side |

**#6 and #7 have no remote equivalent** — cumulative realized-premium and tax-reserve
figures exist only in the local ledger and aren't derivable from Webull's position API.
Practical mitigation: the ledger currently holds **0 fills and 0 closed trades**, so the
blocker is architectural, not a data migration. Defer them cheaply.

Cross-cutting: `get_playbook_performance()` reads `data/conviction_journal.json` for
size calibration. Soft — degrades to zero adjustment if absent.

**Follow supermcp's existing scan shape**, which is good and already proven by
`wallscan.py`: pure scoring function → async orchestrator with rate-limit pacing →
dedup-on-write JSONL log (`scanlog.record`) → thin tool wrapper. The dedup key
`(date, setup, symbol, wall, trigger)` is what makes the log falsifiable rather than
noisy. Ported playbooks should log the same way so they can be graded the same way.

**Two frictions to decide on:**

- **`app.py` is 1253 lines of flat `@mcp.tool` decorators with no registry.** Adding ~8
  playbook tools means either growing that file substantially or introducing a
  registration pattern that doesn't exist yet.
- **`docs/DATA_POLICY.md` says order tools resolve reference prices only from held
  positions or caller-supplied values — never a live quote fetch**, because exchange data
  is licensed and non-redistributable. Vesper's playbooks fetch live option chains and
  snapshots from Webull. For owner-only personal use this is likely fine, but these tools
  would live on a server that also serves subscribers. **Owner-scoping these tools is a
  policy requirement, not just good hygiene** — and per Phase 5, that mechanism doesn't
  exist for MCP tools yet.

Tests that should travel: `test_0dte_playbook.py` (24), `test_adx_iv_router.py` (11),
`test_earnings_vega_harvest.py` (10), `test_collar_following.py` (7),
`test_tax_reserve_sweep.py` (8), `test_premium_recycling.py` (5), `test_thega.py` (4),
`test_risk_and_bounce.py` (8).

---

# Phase 8 — What retires, what keeps running

Explicitly **not** a big-bang cutover. Several Vesper pieces have no clean home in
supermcp and should keep running as-is, possibly indefinitely.

**Keeps running in webull-sidecar (no home in supermcp today):**

- **`mcp_server/` — the local `momentum` connector, 56 read-only tools.** Its dependency
  tree (chromadb, pandas-ta, matplotlib, trafilatura, tradingview-screener) is far heavier
  than supermcp's deliberately-thin requirements. Keep it local over stdio. It holds no
  broker credentials, so it carries no execution risk.
- **Backtesting suite, 139-book knowledge search, conviction journal, paper ledger** —
  local state and heavy deps, no execution authority.
- **`vesper loop` in `dry_run`/paper** as a playbook development harness, until Phase 7
  completes.

**Retires, in this order, only after the phase that replaces it lands:**

| Component | Retires after |
|---|---|
| `vesper/bot/` Telegram + Discord approval adapters | Phase 5 |
| `vesper/nodes/human_gate.py`, approval registry | Phase 5 |
| `vesper/loop.py`, `alerts_runner.py`, `stream_runner.py` | Phase 6 |
| `vesper/graph.py`, `runner.py`, the LangGraph node pipeline | Phase 7 |
| Vesper's live Webull write path (`execution_guard` as *the* order path) | Phase 2 |

**Already dead, don't revive:** `deploy/` (stale pre-migration systemd/port-8787),
`vesper/bot/inbound.py`'s aiohttp webhook app (nothing starts it).

**Undecided:** whether `stream.py`'s gRPC push feed can run from vultr at all. If not,
the monitor degrades to 15s polling — acceptable but materially worse for a -40% 0DTE
stop.

---

# Phase 9 — Documentation

**Update each doc when its phase actually lands, never before.** This repo's standing
norm is that a stale CLAUDE.md is worse than none — and there is direct precedent here:
stale docs previously propagated into a contributor's plan and cost real work.

| Doc | What changes | After phase |
|---|---|---|
| `CLAUDE.md` rule 1 | network stance — supermcp is authenticated and internet-facing, unlike anything described today | 4 |
| `CLAUDE.md` rule 3 | "the order path lives in exactly one file" becomes false the moment guards exist in two repos, and true again only when Vesper's retires | 2, 8 |
| `CLAUDE.md` rules 4b/4c | monitor + alert watcher relocate | 6 |
| `CLAUDE.md` rule 4d | the voice/approval reversal | 5 |
| `CLAUDE.md` rule 6 | if the LLM may originate trade ideas, this rule changes meaning | 5 |
| `CLAUDE.md` Layout + Status | modules retire | 8 |
| `ROADMAP.md` | record the nyx-rejection reversal *with reasoning*, per the repo's own norm of documenting reversals | 5 |
| `docs/VOICE_STACK_GUIDE.md` | transport changes from Telegram voice notes to Discord/Nyx; the model layer stays valid | 5 |
| `docs/SUPERMCP_VS_VESPER_TOOLS.md` | refresh the inventory as tools move | 7 |
| supermcp `CLAUDE.md` + `DATA_POLICY.md` | new guard/risk modules; owner-scoping for playbook tools | 2, 3, 7 |

---

# Open decisions

Nothing below should be implemented until Michael has ruled on these. The first three
are blocking; the rest can be decided as their phase approaches.

1. **Does a spoken ticket-ID confirm-back genuinely replace the button approval?**
   This reverses rule 4d, rule 3's approval model, and the 2026-08-28 nyx decision.
   The proposed design keeps a machine-checkable binding between approval and payload
   while dropping the app-switch — but it is a real relaxation of a deliberate rule on a
   live account, and needs an explicit yes, not an inferred one.
2. **Phase 0 — close the ungated Webull path now?** `UNGATED_ORDER_BROKERS=webull`
   waives both gates today, with $10k/1000-share caps. This is independent of everything
   else and is the single largest open exposure.
3. **Should the jarvis hub keep raw `Bash` + `bypassPermissions` once it can reach an
   order path?** Tool-level guards are decoration if the session can `curl` past them.
4. **How far does rule 6 relax?** "The LLM may narrate, reject, or shrink — never
   originate or increase" was deliberate and re-affirmed. Voice-native idea generation
   implies the LLM *originates*. Does it also get to size? Recommendation: originate and
   discuss freely, but sizing stays deterministic and the caps stay silent and
   server-side.
5. **Ticket TTL for voice** — extend past 120s, or keep it short with cheap re-preview?
   Long TTLs let a stale price get confirmed.
6. **Durable ticket store, or pinned single worker?** Both are defensible; the current
   implicit single-worker default is not.
7. **Playbook data design — injected audits (a) or vendor the analyst layer (b)?**
   Determines how much of `mcp_server/` moves.
8. **Does the blast-radius objection from 2026-08-28 still stand?** disclaw also runs
   business automation on a shared, budgeted Claude runner. If it no longer stands, say
   why on the record; if it does, trading may need its own hub or its own runner.
9. **✅ DECISION MADE: Token RBAC / Owner-scoping for MCP tools**. Michael confirmed he wants live trading available on the remote server but strictly scoped to his keys only, with view-only for subscribers. **Action:** We must implement Role-Based Access Control (RBAC) in `supermcp`'s `auth.py` / FastMCP token validation. `place_live_order` (and `dry_run_order`) will check the active token scope and reject any non-admin (subscriber) tokens with a 403. Tastytrade view-only logic remains safe as it only pulls delayed marks.
10. **Do #6/#7 (Premium-Recycling, Tax Reserve Sweep) get a remote ledger, or stay local
    indefinitely?** Cheap to defer today — the ledger is empty.

---

# Addendum: MCP Tool Packaging & Coolify Deployment (2026-09-01)

To prepare for folding Vesper's quant intelligence into `supermcp` without copying 800 lines of flat decorators into `supermcp/src/app.py`, the tool suite has been modularized and packaged.

### Remote Environment
- **Host:** `ssh coolify` (Vultr server)
- **Live codebase:** `cd supermcp` (runs under systemd / FastMCP at `https://mcp.mphinance.com/mcp`)

### Tool Tiering & Modular Registry Architecture
The 56 tools from `mcp_server/` are categorized and registered via `mcp_server/registry.py`:

| Tier | Scope | Count | Dependencies | Status in Package |
|---|---|---|---|---|
| **Tier 1** | Pure REST, TDPro Flow, SEC EDGAR, News, Sizing | 19 tools | `requests`, standard lib | Packaged (`register_tier1_tools`) |
| **Tier 2** | Market Regime, Breadth, Screeners, Technicals | 24 tools | `yfinance`, `pandas-ta`, `scipy` | Packaged (`register_tier2_tools`) |
| **Tier 3** | VoPR Options Analytics Engine | 4 tools | `scipy`, `numpy` (Black-Scholes) | Packaged (`register_tier3_tools`) |
| **Tier 4** | Knowledge Base (139 books RAG) & Backtester | 9 tools | `chromadb`, heavy vector deps | Kept local / standalone |

Total tools ported: **47 tools**.

### Packaging Artifacts
An automated packaging script (`scripts/package_for_supermcp.py`) generates:
1. `dist/supermcp_tools/mcp_server/` — Clean, ready-to-import Python package with `registry.py`.
2. `dist/supermcp_requirements.txt` — Minimal dependencies needed on the remote host (`fastmcp`, `tradingview-screener`, `pandas-ta`, `yfinance`, etc.).
3. `dist/supermcp_momentum_tools.tar.gz` — Tarball for deployment transfer.

### Deployment Steps to `supermcp`

1. **Copy tarball to Coolify host:**
   ```bash
   scp dist/supermcp_momentum_tools.tar.gz coolify:~/supermcp/
   ```

2. **Extract & install dependencies on remote host:**
   ```bash
   ssh coolify
   cd supermcp
   tar -xzf supermcp_momentum_tools.tar.gz -C src/
   pip install -r supermcp_requirements.txt
   ```

3. **Mount in `supermcp/src/app.py`:**
   ```python
   from src.mcp_server.registry import register_momentum_tools

   # Mounts all 47 quant, flow, regime, and options tools:
   register_momentum_tools(mcp)

   # (Optional) Mount selectively by tier:
   # register_momentum_tools(mcp, include_tiers=(1,))      # Tier 1 only (zero heavy deps)
   # register_momentum_tools(mcp, include_tiers=(1, 2))   # Tier 1 + Tier 2
   # register_momentum_tools(mcp, include_tiers=(1, 2, 3))# All tiers including options engine
   ```

4. **Required Remote Environment Variables (`.env`):**
   - `TDPRO_API_KEY` (or `TD_API_KEY`): TraderDaddy Pro API access.
   - `SEC_USER_AGENT`: SEC EDGAR header compliance (e.g. `MomentumAdmin admin@mphinance.com`).

