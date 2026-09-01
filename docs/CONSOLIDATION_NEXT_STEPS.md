# Consolidation — next-steps checklist (written 2026-09-01, pre-handoff)

Written before handing deploy/coding work on `docs/SUPERMCP_CONSOLIDATION_PLAN.md` to
another model, as a fixed reference point. When work comes back, diff it against this —
not against the plan doc, which describes the *what*; this describes the *order* and the
*acceptance criteria* I'd have used.

Ranking: independent safety fixes first, then the single-owner client, then guards,
before anything additive (playbooks) or anything requiring your sign-off (voice-approve).

---

## 0. Do first, independent of everything else

These close exposures that exist right now on `vultr`, regardless of whether
consolidation happens at all.

- [ ] **`UNGATED_ORDER_BROKERS` no longer contains `webull`**, or if it's kept, the
      adapter's own caps are deliberately lowered to match (see next item) — not left at
      $10k/1,000 shares as an accidental default.
      **Verify:** `ssh coolify` (or vultr) — `grep UNGATED_ORDER_BROKERS` in the running
      env; confirm a Webull order now actually requires `LIVE_ORDERS_ENABLED` and the
      `"SEND IT LIVE"` phrase to arm.
- [ ] **Notional/quantity caps aligned** to $2,500 / 25 (Vesper's numbers) until there's
      a deliberate decision to raise them — not $10,000 / 1,000.
      **Verify:** read `WEBULL_MAX_ORDER_NOTIONAL_USD` / `WEBULL_MAX_ORDER_QUANTITY` (or
      wherever they end up) directly, don't take a changelog's word for it.
- [ ] **`uvicorn` worker count pinned explicitly** (`--workers 1`) with a comment saying
      why — every design below (ticket store, monitor thread, existing warmer thread)
      breaks or duplicates silently if this ever becomes >1 without a redesign.
      **Verify:** `systemctl cat supermcp` or the equivalent unit file/ExecStart line.
- [ ] **SDK pin reconciled** — Vesper pins `webull-openapi-python-sdk==2.0.16`, supermcp
      pins `2.0.12`. Pick one before any shared-client work (item 1 below) starts; doing
      the client merge first and reconciling pins after is backwards.
      **Verify:** diff the two `requirements.txt`/`pyproject` pins.
- [ ] Jarvis hub fencing is a **conscious choice**, either direction — either
      `Bash`/`WebFetch`/`Task` get denylisted on the trading channel (mirroring
      `ambient.ts`'s existing pattern) or someone explicitly decided raw shell there is
      fine and wrote down why. What it must not be is untouched-by-default.
      **Verify:** read disclaw's hub config for the jarvis/`#supermcp` channel; check for
      an `extraDisallowedTools` entry or an explicit comment.

**Red flag if I check back and find:** any of the above still defaulted, *or* fixed by
someone quietly deleting the safety margin the other direction (e.g. raising Vesper's
caps to match supermcp's instead of lowering supermcp's to match Vesper's).

---

## 1. One Webull client owner (resolves F2)

- [ ] `brokers/webull.py` on supermcp gets **the same rate-limit discipline as `wb.py`**:
      a lock, backoff, and stale-fallback specifically for the 2 req/2s order-query
      bucket. Market-data calls stay on the separate, generous-bucket path — the two
      must **not** get merged into one client.
- [ ] **No more swallowing failures into empty results.** A rate-limit error, a timeout,
      and "genuinely zero positions" must be three distinguishable outcomes, not one.
      **Verify:** find the old blanket `except Exception: return []`/`return {}` and
      confirm it's gone — grep is enough, this is a mechanical check.
- [ ] Until Phase 1 is fully done, confirm **only one of {`vesper loop`, supermcp} is
      hitting the live order-query bucket at a time** — either Vesper stays in
      `dry_run`/paper, or they're on non-overlapping schedules. Two processes on two
      machines cannot share an in-process lock; this has to be operational, not code.
      **Verify:** check `vesper loop`'s actual running mode (`VESPER_TRADING` env) on
      whatever box runs it.

**Red flag:** a "fix" that adds retries without adding backoff (retries alone make a
rate-limit collision *more* likely, not less), or one that merges the order-query and
market-data clients "for simplicity."

---

## 2. Guard + ticket handshake ported (resolves F1)

- [ ] `execution_guard.py`'s guard logic lifts into `src/guard.py` (or wherever) —
      confirm it's actually reused/ported, not reimplemented from a description. It's
      already broker-agnostic (`place_fn` closure, plain dicts), so a rewrite here is a
      red flag by itself — there's no reason not to port the real file.
- [ ] **Strike-based sizing for SELL-to-open options is present and tested.** This is
      the single highest-value item to verify by hand: construct a short-put payload with
      a strike well above the notional cap and confirm it's rejected. This exact bug
      (reading `limit_price` instead of `strike`) let a $19k risk past a $2.5k cap once
      already — it will recur if this is rewritten instead of ported.
- [ ] **Ticket store is disk-backed**, not an in-memory dict — this repo already paid
      for learning that lesson once (2026-08-29, LangGraph checkpointer + approval
      registry). A `_tickets = {}` module-level dict under uvicorn is the exact bug
      shape to look for.
      **Verify:** grep for the ticket store's definition; confirm it reads/writes a file,
      not just a dict, and survives a process restart in a manual test.
- [ ] Multi-leg formula whitelist ported with **exactly the registered strategies**
      (`SYNTHETIC_LONG`, `THEGA`) — an unregistered `strategy_type` must be refused, not
      approximated. Don't let "let's just support more strategies while we're in here"
      creep in without the same refuse-if-unregistered discipline.
- [ ] Kill switch defaults **off**, confirmed by reading the actual default, not the
      variable name.
- [ ] Ported tests actually run and pass against the new location:
      `test_execution_guard.py` (31), `test_multileg_execution.py` (9),
      `test_execution_integration.py` (8). If these didn't travel, the port isn't done —
      it's a rewrite with no safety net, no matter how confident the diff looks.

**Red flag:** any version of this phase that ships without the strike-vs-premium test
passing, or with the ticket store still in-memory "for now."

---

## 3. Portfolio-level risk

- [ ] Lives in a new module called from the execution path (`orders.py`'s `execute`),
      **not** folded into `brokers/webull.py` — check this by import graph, not by
      reading a summary. If sector concentration / circuit breaker logic is inside the
      Webull adapter file, it's scoped to one broker by construction and the whole point
      is lost.
- [ ] `peak_nlv` is **re-seeded against the live account**, not carried over from the
      paper account's `100000.0` default. This is a specific, checkable number — read
      whatever `circuit_breaker_state.json` (or equivalent) actually contains after
      first run and confirm it's not exactly 100000.0.
- [ ] `halt_state` and `circuit_breaker_state` stay **separate files/keys** — not merged
      into one "are we ok" boolean.
- [ ] `yfinance` (needed by `sector.py`'s ticker→sector lookups) actually gets added to
      supermcp's dependency list — don't assume it's already there; the plan flagged it
      as absent.
- [ ] Tests travel: `test_sector_concentration.py` (17), `test_portfolio_governance.py`
      (19), `test_circuit_breaker.py` (8).

---

## 4. Jarvis hub — read-only wiring only

- [ ] supermcp registered as an actual MCP server for the trading channel (not just
      raw shell access to `curl` it).
- [ ] `Bash`/`WebFetch` denylisted on that session **once MCP tools exist** — otherwise
      the MCP wiring is decoration.
- [ ] Confirm **no order-placing tool is reachable** from this hub at the end of this
      phase — P&L/positions/gamma-flip/what's-open queries only. This is the one item
      worth manually testing by voice myself before considering it done.
- [ ] `bypassPermissions` status revisited and the decision (keep or drop) is written
      down, not silently left as-is.

**Red flag — this is the one to check hardest:** any order-capable tool exposed to this
hub, or Phase 5 (spoken confirm) implemented at all, without your explicit sign-off
first. The plan is explicit that decisions 1–3 are blocking. If I check back and
`place_live_order` (or equivalent) is reachable from a voice session, that's a stop-ship
finding regardless of how well everything else was built.

---

## Do NOT expect done without your sign-off first (Phase 5+ and related)

If any of these show up in what comes back, flag it — these were listed as blocking
open decisions, not defaults:

- Spoken ticket-ID confirm replacing button approval (reverses rule 4d + the
  2026-08-28 nyx decision).
- Rule 6 relaxed to let the LLM size a position, not just narrate/reject/shrink.
- `bypassPermissions` left on for a hub that can now reach an order path.
- Ticket TTL extended past 120s without a re-preview mechanism to keep prices fresh.

---

## Playbooks (Phase 7) — lowest priority, purely additive

Not worth reviewing in detail until Phases 0–3 are solid. When it's time:

- [ ] Confirm the injected-audits-vs-vendor-analyst-layer decision was actually made
      (open decision #7), not defaulted to whichever was easier to code.
- [ ] `#6` (Premium-Recycling) and `#7` (Tax Reserve Sweep) should still be deferred —
      the local ledger is empty, there's no urgency to solve this.
- [ ] Owner-scoping exists on any playbook tool that fetches live option chains, per
      `DATA_POLICY.md`'s reference-price rule — this is a policy requirement on a
      server that also serves subscribers, not optional hygiene.
- [ ] Relevant playbook test files travel with the code (see the plan doc's list) —
      same principle as Phase 2: a port without its tests is a rewrite.

---

## How I'll actually check when you ask me to

1. Diff current `UNGATED_ORDER_BROKERS`, caps, worker count, SDK pins against the "Do
   first" section above — these are fast, mechanical, and catch regressions immediately.
2. Read the new guard/ticket code directly and look for the ported tests, not a
   description of them. Run them if I can.
3. Manually construct the strike-vs-premium payload and confirm it's rejected — don't
   trust a summary that says "guards were ported."
4. Check whether anything in the "Do NOT" section shipped anyway.
5. Only then look at playbook coverage/breadth — it's real value but it's not where the
   risk is.
