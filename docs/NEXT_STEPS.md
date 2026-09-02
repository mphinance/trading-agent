# Vesper — the plan from here

**29 of 110 features done. 628 tests passing. The MCP server is live and connected.**

This is the working plan: what is true today, what happens next and in what order, which
parts are yours, and which risks are still open. It supersedes the ordering in
`autonomous/prompts/app_spec.txt` where the two disagree — that spec predates the
MCP/agent split, the Discord decision, and the order-path decision.

---

## 1. Where things actually stand

### Verified working

| | |
|---|---|
| `https://agent.mphinance.com/mcp` | TLS valid, 60 tools, MCP handshake succeeds, connected to Claude |
| Auth | unauthenticated → 401; valid bearer → 200; OAuth discovery live with PKCE S256 |
| Network posture | bound `10.0.0.1:8500`, **zero** `0.0.0.0` binds, ufw allows only `10.0.1.0/24` |
| Process isolation | invoking all 13 Vesper tools pulls in **zero `vesper.*` modules** |
| CI | green on 3.12 and 3.13 |
| Discord | bot `mph-trading-bot` in the guild, `#vesper-approvals` private, cards render |

That third row is the one worth re-reading. Before M0, calling a single "read-only" MCP tool
loaded `vesper.execution_guard` — and with it the live `ExecutionGuard` object — into the
internet-facing process, along with the Telegram adapter. The AST pin was correct that nothing
*called* the order path; it could not see that the order path was *present*. It no longer is.

### Milestone state

```
M1  Repo cloneable again          7/7    done
M0  The core/ split              10/10   done
M2  OAuth 2.1                    11/11   done
M8  Voice co-pilot + order path  23/23   done
M10 Skills endpoint               0/7    NEXT
M7  Deployment: two units         0/9
M3  Credentials on the box        0/7
M4  Full Vesper running remotely  0/10
M5  Discord approvals             0/9
M6  Soak, then arming             0/7
M9  Docs tell the truth           0/6
H   Human-only                    2/4
```

---

## 2. Decisions already made — do not re-litigate these

1. **One repo, three layers.** `core/` (leaf) → `mcp_server/` + `trading_mcp/` → `vesper/`.
   Two deployables from one checkout. Two repos was rejected because the rule-3 AST pin only
   works when it can see both packages in one tree.
2. **Voice is claude.ai over the MCP connector.** CLAUDE.md rule 4d's Telegram-voice-note
   design is cancelled. No STT, no audio endpoint, ever, in this repo.
3. **Discord, not Telegram**, in a dedicated private approvals-only channel. Exactly one
   channel may be configured — `channel_manager` broadcasts to *every* configured adapter, so
   two would mean two approval surfaces for one irreversible action.
4. **OAuth 2.1 with CIMD primary, DCR fallback, static bearer as the escape hatch.**
5. **The MCP may place orders** — `submit_manual_proposal`, `place_from_ticket`, and
   `place_order` — bounded by the deterministic guards and a separate `trade` scope. Capital
   in the account is kept low deliberately as the outer bound.
6. **Paper mode is skipped.** M6 becomes soak-and-arm without a paper phase.
7. **A dedicated box before live.** Paper/dev on coolify is fine; `VESPER_TRADING=1` should
   not happen on a machine that also runs Coolify and other people's containers.

---

## 3. The order, and why

**M8 → M10 → M7 → M3 → M4 → M5 → M6 → M9**, with M2's follow-up folded into M8.

The ordering is dependency, not importance:

- **M8 needs M0** (done) — the watch tools read state through `core`, which is why the MCP can
  stay up when the agent is down.
- **M10 needs M8** — the skills endpoint describes the tools, so the tools must exist first.
- **M7 needs M8/M10** — you deploy what exists; rewriting the deploy first means rewriting it twice.
- **M3 before M4** — the agent cannot run without credentials.
- **M5 needs M4** — approvals need something generating proposals.
- **M6 last** — you can only soak a complete system.

### M2 follow-up (fold into M8)

`/.well-known/oauth-protected-resource` returns 404. That is the RFC 9728 resource-metadata
document; a client uses it to discover *which* authorization server protects a resource.
Add it before switching the connector off bearer auth.

---

## 4. M8 — the next milestone (23 features)

Three groups.

### The watch surface — the actual voice use case

You see a proposal in the queue that is not ready to buy. Rather than watching your phone,
you get on a call and watch the 5-minute chart together until it triggers.

- `watch_setup(proposal_id)` — one small speakable payload: thesis, entry/stop/target, current
  price, **distance to trigger in % and $**, a summarised read of recent 5m bars, VWAP, nearby
  dealer-gamma levels.
- `describe_intraday` — structure in words. *"Third consecutive higher low, 0.4% under the
  trigger, volume half the 20-bar average."* Voice cannot see a chart.
- **Repeat-call suppression** — ask twice in ninety seconds with nothing changed and it says so
  compactly instead of re-reading the thesis. This is the difference between a tolerable
  twenty-minute call and an infuriating one.
- Fuzzy symbol resolution that **says back what it matched** (NVDA → "in video" is a real,
  repeatedly-observed failure).
- Payload bounds on `get_account_state` and `get_audit_trail`, which are unbounded today.

### The safe-write tools

`arm_alert` / `disarm_alert`, `snooze_proposal`, `tag_proposal`, and `halt`. All
exposure-reducing: none can increase risk, so none needs the button.

### The order path

- **M8-19 writes the amendment down first.** The M0-00 lesson: widening an invariant without
  documenting it first produces docs and tests that contradict each other.
- **M8-20 `submit_manual_proposal`** — your manual order through the full deterministic gate.
  Returns a staged ticket, or the specific guard that rejected it and its threshold.
- **M8-21 `place_from_ticket(ticket_id)`** — takes only an id, never a payload. Preserves the
  property that no single call both constructs and fires an order.
- **M8-22 `place_order`** — direct, one call, with MCP-specific per-order and per-day caps,
  both env-configurable and defaulting small.
- **M8-23** — a `trade` scope the read token does not carry, and the AST pin rewritten so
  `guard.place` is reachable from exactly one named module and nowhere else.

Everything routes through `vesper/execution_guard.py`. No order-construction code exists
anywhere else, and that does not change.

---

## 5. Yours, and they are all unblocked right now

| | |
|---|---|
| **Rotate two credentials** | The `TRADING_AGENT_TOKEN` and the Discord bot token both appeared in a streamed transcript. Rotation commands are in the session log; neither is urgent, both are two minutes. |
| **H3 — arm live trading** | `VESPER_TRADING=1`. Never an agent's keystroke. Preconditions land in M6. |
| **H4 — confirm voice on the phone** | Once M8 lands, ask "where is it now" a few times and tell me what reads badly. |
| **Decide on the dedicated box** | Needed before H3, not before anything else. |

---

## 6. Open risks

**An unexplained Vesper instance sent execution reports.** Paper mode, no real order, but
it ran `monitor.py`'s exit cascade with a loaded `.env` from somewhere that is not any of your
seven known hosts, not CI, and not any agent. The bot is deleted and the token removed, so it
is silenced — not located. **This matters before M4**, because that milestone starts a second
monitor. Two monitors on one account means duplicate exits and a 2-req/2s bucket collision.

**Two ports are exposed to the internet past ufw** — `sam-dashboard:8400` and Traefik's
dashboard on `8080`, via Docker's FORWARD-chain bypass. Not this project's, but on the box this
project now lives on. See `docs/COOLIFY_MAP.md`.

**coolify is at 77% disk** with 2.2 GB swap in use, and M4 adds two processes plus possibly
chromadb. M4-03 measures before starting.

**Agents rewrite `feature_list.json` from stale reads.** Twice this destroyed work. Mitigations:
agents are forbidden from touching it, verifiers check whether they did anyway, and edits are
committed immediately. `autonomous/feature_list.seed.json` is the source of truth.

---

## 7. How the work runs

One workflow per milestone, launched after reading current state — not one long script with a
baked-in feature list that goes stale.

Each feature: **implement (Sonnet) → verify (Haiku, independent) → repair once if rejected →
skip and record if it still fails.** The verifier re-runs the suite itself, spot-checks one
claim **against the source rather than against a doc**, and re-checks that M0's zero-vesper-modules
property still holds. A milestone audit at the end asks the only question that matters: *is any
feature marked passing whose property does not actually hold?*

That structure exists because of three real failures today: a feature marked passing on a false
premise, a security invariant widened because two documents agreed with each other, and a
workflow that halted on feature one because a single rejection stopped everything.

---

## 8. What done looks like

A proposal appears in `#vesper-approvals`. You are not at your desk. You open Claude on your
phone, say *"what's the NVDA setup doing"*, and hear the distance to trigger and the shape of
the last few bars. You wait. You ask again. When it triggers you press **Approve** — or say
"place it" and it goes through the gate with the caps enforcing themselves.

No laptop involved. Nothing running that you did not start. Every decision in the audit chain.
