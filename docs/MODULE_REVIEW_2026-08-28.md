# Module Review — Modules 2, 3, 6 (2026-08-28)

Reviewed the three most recently shipped feature modules while a concurrent
session (Gemini Flash, same working directory) kept building ahead — Module 2
(channel-agnostic bot engine), Module 3 (active position monitor & exit
cascade), and Module 6 (leveraged-ETF proxy + skills evolution). Goal: catch
the kind of precision bug a fast/cheap model produces before it reaches a live
account, fix what's fixable directly, and leave the rest as findings.

Full suite: **50/50 passing** after fixes (`pytest -q`). Everything below is
already committed.

## Fixed

### 1. Module 3: live exit cascade called the guard with the wrong signature — guaranteed crash

`vesper/monitor.py`'s `execute_exit_cascade()` called
`guard.preview(symbol=..., side=..., quantity=..., price=..., broker=...)` and
checked `ticket.ok` / `placed.ok` / `placed.order_id`. None of that matches
`vesper/execution_guard.py`'s actual API
(`preview(proposal_id, payload_dict, live_buying_power)` →
`Ticket(id, proposal_id, digest, created_at, used)`,
`place(ticket_id, payload, place_fn)`). Every one of those calls would have
raised `TypeError` before the guard's own logic — including the kill switch —
ever ran.

Because `run_monitor_loop`'s outer loop wraps each cycle in a broad
`try/except`, this wouldn't have crashed the process. It would have logged an
error and moved on — **after already broadcasting "🚨 EXIT CASCADE:
liquidating $X" to Telegram/Discord**, since that alert fires before the
broker call. A real stop-loss or take-profit trigger would tell you it was
closing the position and then silently fail to do so.

No test caught this because none exercised `live=True` — every existing test
in `tests/test_monitor.py` called `evaluate_position()` (pure logic, no I/O)
or `execute_exit_cascade(trigger, live=False)` (the dry-run branch, which
never touches the guard).

**Fix:** rewrote the live branch to call `guard.preview()`/`guard.place()`
with the real signature, matching the pattern already used in
`vesper/nodes/executor.py`. Also made `BLOCKED_BY_GUARDRAIL`/`FAILED` results
broadcast to the bot channels too — previously only a successful `SUBMITTED`
result would (via the code path that could never be reached), so a blocked or
failed exit would have gone from "we're liquidating this" to total silence.

**Tests added:** `test_execute_exit_cascade_live_places_guarded_order`
(asserts the real `place_order` call happens with correct args) and
`test_execute_exit_cascade_live_blocked_by_kill_switch` (asserts
`VESPER_TRADING` defaulting off blocks it and the broker is never called).

### 2. Module 3: option detection checked a key that doesn't exist

`poll_webull_positions()` checked `p.get("asset_type") == "OPTION"` to decide
whether a Webull position is an option (for correct P&L multiplier and 0DTE
time-stop eligibility). `wb.py`'s `_position()` never emits an `asset_type`
key — the actual field is `instrument_type`. This silently degraded to the
`len(symbol) > 6` heuristic alone, every time. Fixed to read the right key.

### 3. Module 3: a failed exit attempt erased the position's protective state

`run_monitoring_cycle()` deleted a position from `self.tracked_positions`
after *any* exit attempt, success or not. `tracked_positions` is where
`peak_gain_pct`/`breakeven_locked` live — the state that remembers "this
position already hit +25% and is now protected by a breakeven stop." If a
live sell failed (including via bug #1 above) or got blocked by the guard,
deleting the tracking entry meant the *next* poll cycle would re-add the
still-open position from scratch, with `breakeven_locked` reset to `False`.
An already-armed protective stop would silently disappear the moment an exit
attempt on it failed. Fixed: only clear tracking on `SUBMITTED` /
`DRY_RUN_SIMULATED`.

### 4. Module 6: leveraged-ETF proxy proposals used the *underlying's* price

`playbooks_node()`'s "2x leveraged alternate" logic drafted a `LIMIT` order
for a completely different ticker (e.g. `NVDL` for an `NVDA` signal) but set
`limit_price=entry_price` — NVDA's price, not NVDL's. The `estimated_cost`
was computed the same fabricated way (`shares * entry_price * 0.5`, an
"approximate vehicle price" comment admitted as much). That number doesn't
just look wrong in a UI — it flows directly into `ExecutionGuard`'s notional
cap check as if it were real, meaning the one guard whose entire job is
"don't let a bad number through" would have been evaluating a bad number.

**Fix:** added `_fetch_live_quote()` (via `md.Market`, the 600 req/min
market-data bucket, not the scarce order-query one) and skip the proxy
proposal entirely — no proposal, not a wrong one — if a real quote isn't
available. Stop-loss/target for the proxy are now derived from its own real
price instead of a scaled copy of the underlying's.

**Also found while there:** `playbooks_node()` had its own hardcoded
`account_equity = 10000.0` for position sizing — a second, independent
instance of the exact bug Module 0 fixed in `risk_gate_node`. Extracted the
live-equity fetch into `vesper/account.py` so there's one implementation
instead of two copies drifting apart; `risk_gate_node` now imports it too.

**Tests added/updated:**
`test_playbooks_node_generates_2x_leveraged_proposal` now mocks a real quote
instead of asserting on a fabricated one; added
`test_playbooks_node_skips_proxy_without_live_quote`.

## Reviewed, found clean

**Module 2's bot layer** (`vesper/bot/{base,manager,telegram_adapter,
discord_adapter,webhook_adapter}.py`) is send-only today — every adapter
pushes proposal cards, execution results, and alerts outward. There is no
callback *receiver* yet for the Telegram/Discord Approve buttons (grepped for
`callback_data`, `human_gate_node`, interrupt-resume patterns — nothing wires
an inbound tap to execution). `human_gate_node` still gates every proposal
through LangGraph's `interrupt()`, and `executor_node` still routes through
`execution_guard`. **This remains the single most dangerous piece of code not
yet written**: when the callback receiver gets built, it must resume the
graph's interrupt (`human_decision = "APPROVE"`) rather than call
`executor_node` or a broker directly, or it will simultaneously bypass both
the human-approval step and the ticket handshake. Flagged in
`NEXT_STEPS.md`'s gotchas already; repeating it here because it's the next
thing likely to get built fast.

## Found, not fixed (lower severity, time-boxed out of this pass)

**Module 1** (`vesper/morning.py`) falls back to hardcoded placeholder SPY/QQQ
levels (`769.35` / `768.62` etc.) if TDPro is unconfigured or the fetch fails,
with no "STALE" or "UNAVAILABLE" label distinguishing a real number from a
fallback one. This is read-only (no money moves), but it's the same failure
mode CLAUDE.md's rule 4d already named for the deleted `alerts.py`: a
fabricated number that looks real is worse than an honest gap. Worth a
follow-up label, not urgent.

## Provenance note

This review happened while a second session was actively committing to the
same working directory in real time — Modules 1, 2, 3, and 6 all landed
between the previous review and this one. Nothing here should be read as a
complaint about that pace; the bugs found are exactly the shape you'd expect
from moving fast, and all four were caught before `VESPER_TRADING` was ever
set to `1` anywhere.

---

## If Tony Stark opened this repo

Not asked for, but fun to think about: what's actually missing between "runs
scripted modules on demand" and "JARVIS quietly has your back." Roughly in
order of how much it'd change daily use:

1. **One brain, not seven modules you invoke by hand.** Right now `morning`,
   `monitor`, `scan`, and the bot layer are separate CLI verbs you remember to
   run. JARVIS never waited to be asked. The natural next step is a single
   long-running process where the morning brief, the scanner, and the
   position monitor are nodes in one always-on graph, not things a human
   remembers to `vesper morning` at 8:45.
2. **Memory that outlives a process.** `data/conviction_journal.json` exists
   but nothing reads it back in — Module 5 on the roadmap (auto-resolve a
   proposal's outcome at 1/5/10 days, feed the hit rate back into
   `Candidate.score`) is the difference between a system that repeats its own
   mistakes forever and one that visibly gets sharper. This is the single
   highest-leverage unbuilt piece.
3. **A voice that can push back, not just narrate.** The bot cards are
   one-directional today (proposal → approve/reject). JARVIS argued with Tony.
   A conversational layer where you can ask "why this trade" and get the
   actual `regime`/`technicals`/`conviction` state back — not a canned
   thesis string — turns the bot from a notifier into something worth
   talking to.
4. **Situational awareness that doesn't need polling.** The monitor loop
   sleeps and re-checks every 15s. Webull already has gRPC trade-event push
   (the pre-migration sidecar used it — see `docs/CODE_SWEEP_2026-08-28.md`).
   Push-driven position/fill events would cut the loop from "notice within
   15 seconds" to "notice immediately," which matters a lot more at 3:59 PM
   on a 0DTE than it sounds like on paper.
5. **It should know what regime it's in without being told.** `regime_node`
   exists but a lot of sizing math still uses static constants
   (`STOP_LOSS_0DTE_PCT = 0.40` etc.) regardless of whether the tape is calm
   or the VIX term structure is inverted. JARVIS scaled the suit's behavior to
   the threat level; this system could scale position sizing and stop
   distance to the *current* regime score instead of a fixed table.
6. **Failure should be loud, not logged.** More than one bug in this review
   (#1 and #3 above) was dangerous specifically because it failed *quietly* —
   an error into a log file nobody's tailing at 2pm on a Tuesday. A real
   JARVIS-grade system pages you the moment `run_monitor_loop`'s except
   branch fires, not just when a trigger fires successfully.
7. **The kill switch should be reachable from anywhere, instantly.** Module 7
   already plans a `vesper halt` / `/halt` bot command — that's the right
   idea. The version worth building is one where saying "stop trading" out
   loud to Claude Desktop over the existing MCP-adjacent voice path (rule 4c
   in the old sidecar's CLAUDE.md) kills `VESPER_TRADING` in under a second,
   because the whole point of a kill switch is that it works when you're not
   at a keyboard.

None of this is urgent — Module 0's guardrails are the actual prerequisite for
all of it, and they're the thing that's actually done and tested. But it's
the shape of "amazingly smart and automated" that's still short a few pieces:
not more modules, but modules that remember, that push back, and that fail
loud instead of quiet.
