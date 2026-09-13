# HANDOFF — onboarding for someone taking over this repo

*Written 2026-09-13. Companion to [`CLAUDE.md`](../CLAUDE.md), not a
replacement for it.*

**The division of labour between these two documents:** `CLAUDE.md` is the
design contract — the seven critical rules, why each exists, and what breaks if
you violate one. It is checked in, it is read by every agent session, and it is
the thing to keep current. *This* document is the orientation: what to do in
your first hour, where things actually run, and which mistakes are expensive.
Read CLAUDE.md's rules 1-3 before you touch anything. Read the rest of it
before you change anything.

**This app places real orders with real money.** That is not a hypothetical —
`VESPER_TRADING=1` on the deployed box as of 2026-09-04, at the operator's
explicit instruction. Everything below follows from that.

---

## 1. The 60-second model

There are three things in this repo and they are easy to conflate.

| | what it is | can it move money? |
|---|---|---|
| **Vesper** | A LangGraph agent. Scans, analyses, drafts an order, risk-gates it deterministically, asks a human to approve over Telegram/Discord, executes, monitors for an exit. | Yes, via one module. |
| **`mcp_server/`** | Portable quant tooling over MCP, stdio. 56 tools. No broker credentials, no order path. | **No**, and that property is load-bearing. |
| **`trading_mcp/`** | Owner-only MCP server, a separate process. 80 tools — the 64 from the registry, 13 read-only views on live account/agent state, and 3 order tools. Deployed and internet-reachable. | Yes, through those 3 tools. |

The agent and the MCP server are two ways into the same system. The agent runs
a pipeline on a schedule; the MCP server lets you *talk* to it from claude.ai
or any MCP host. Both bottom out in the same order path.

**The pipeline**, which is literally `vesper/graph.py`'s edge order:

```
regime → scanner → analyst → swarm → playbooks → synthesis
       → risk_gate → human_gate → executor → reflection
```

`human_gate` is an interrupt. The graph pauses there and a disk-backed SQLite
checkpointer means a run paused at the approval gate survives a restart. That
is why approval works over a chat button and not a live socket.

---

## 2. Your first hour

```bash
git clone <repo> && cd trading-agent
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

./.venv/bin/python -m pytest -q        # expect 823 passing, ~90s, no network
```

The suite is **hermetic** — no network, no broker, no credentials — because a
green build must not depend on TraderDaddy Pro or ntfy.sh being up. If it needs
a credential to pass, that's a bug in the test.

Then, to run anything live:

```bash
cp .env.vesper.example .env && vi .env
./.venv/bin/python vesper.py status    # confirms Webull + TDPro connectivity
```

**Generate every secret. Never copy one.** `openssl rand -hex 32`. This is not
hygiene advice, it is an incident report: on 2026-09-03 the deployed server
faced the public internet for a day with its bearer token set to the literal
placeholder string out of a `.example` file that is committed to a **public**
repo. Everything looked healthy — the service was up, the tests were green, TLS
was valid, and an unauthenticated request was correctly refused. The gate was
simply published. Four guardrails now exist in code rather than in a reminder
(see CLAUDE.md rule 2), but they only help if you don't route around them.

### The CLI

`vesper.py <command>`, where command is one of `scan`, `analyze`, `0dte`,
`morning`, `monitor`, `loop`, `listen`, `alerts`, `halt`, `resume`, `status`,
`paper`, `audit`.

Two worth knowing immediately:

- **`vesper.py halt`** is the emergency freeze. It is the thing you reach for
  when something is wrong. Note that there is **no halt tool on the MCP
  surface** — `get_halt_status` reads the freeze, nothing on that surface sets
  it, despite the server instructions implying otherwise. In an emergency, use
  the CLI.
- **`vesper.py loop`** is the unattended daemon, and it is where the alert
  watcher actually runs. A one-shot CLI process cannot watch anything after it
  exits, so arming an alert from the CLI and then walking away does nothing
  unless `loop` is running.

---

## 3. Where it runs

The box is reachable as `ssh coolify`. The repo lives at `~/trading-agent`.

| unit | state | notes |
|---|---|---|
| `trading-agent.service` | **running**, lingering on | The 80-tool MCP server |
| `vesper-loop.service` | enabled but **stopped** | The agent loop does not run there yet |
| `vesper-listen.service` | enabled but **stopped** | |

All three are systemd **user** units, not system units. A consequence that has
bitten people: **an `export` in a shell does not reach a systemd service.** It
gets its own environment, from `EnvironmentFile=`.

- **Binds** `10.0.0.1:8500` — the docker bridge. Not `0.0.0.0`, and *not
  loopback either*: Traefik is containerised and cannot reach the host's
  loopback. `MCP_HOST` defaults to `127.0.0.1` in code, so widening it is
  always an explicit act.
- **Reachable at** `https://agent.mphinance.com/mcp`, TLS terminated by Traefik.
- **Auth** is a static bearer plus OAuth 2.1, converged in one `MultiAuth`.

### Which env file is live, and why it matters

This has already cost a day of production exposure. On the deploy box there are
up to three candidate files and only one is read by a given service:

| file | read by |
|---|---|
| `~/trading-agent/.env.trading-agent` | `trading-agent.service` (`EnvironmentFile=`). **This is the live one for the MCP server.** |
| `~/trading-agent/.env.vesper` | `vesper-loop.service`, `vesper-listen.service` |
| `~/trading-agent/.env` | **nothing.** `load_dotenv()` finds it, so it silently *fills gaps*. Editing it to rotate a credential changes nothing the service reads. |

`VESPER_TRADING` now lives in **both** `.env.trading-agent` and `.env.vesper`,
and they must be kept in step. A divergence means the MCP surface and the agent
loop disagree about whether trading is live.

---

## 4. The order path — the part to be careful with

**One module can move money: `vesper/execution_guard.py`.** Everything else
reads. If you are adding a broker write, it goes there or it does not go in.

Three properties hold it together, and none is decorative:

1. **Preview → confirm → place.** `preview()` runs the guards and stages a
   ticket carrying a SHA-256 of the exact payload. `place()` takes a
   `ticket_id`, never an order. So no single call can both construct and fire,
   and what was approved is byte-for-byte what reaches the broker. Tickets are
   single-use, 120s TTL.
2. **Guards run server-side on every path.** Notional cap, quantity cap,
   optional symbol allowlist, optional buying-power fraction. A SELL-to-open
   option is sized off the **strike**, not the premium — a cash-secured put
   commits `strike × 100 × qty` on assignment, and reading `limit_price`
   instead once let a $19k risk sail past a $2.5k cap. Multi-leg combos
   dispatch to a whitelist of per-strategy formulas; an unregistered strategy
   is refused outright, never approximated.
3. **Two kill switches.** `VESPER_TRADING=0` defaults off. `core/halt.py` is
   the freeze, checked before anything else, and `core/circuit_breaker.py`
   trips it automatically on a 15% trailing-peak NLV drawdown.

### The line that must not move

**A tool call can originate an order. A tool call can never approve one.**

`resume()` and `ApprovalRegistry.submit_decision()` are unreachable from every
MCP module, with zero exceptions. Approval happens by tapping an inline
Telegram or Discord button — unambiguous, per-user authorised, restart-safe.
The reasoning is in CLAUDE.md rule 4d: a transcript is ambiguous in exactly the
wrong place ("approve" — *which* proposal?).

This is pinned mechanically by `tests/test_trading_mcp.py`, which walks the
AST. **The pin matches attribute *access*, not just invocation, and it has to**
— the live order path passes the bound method (`asyncio.to_thread(guard.place, …)`),
so a pin matching only `Call` nodes shaped `guard.place(...)` would let a new
tool copy the exact idiom the existing path uses and still pass. It now also
resolves import aliases, dotted access, and `getattr(guard, "place")`. Don't
narrow it back.

### The scope asymmetry that looks like a bug

The static bearer carries `["read", "safe-write"]` and deliberately **not**
`trade`, so the long-lived secret on disk cannot place an order. Only a token
minted through the human-present `/authorize` gate can. The symptom: a bearer
`tools/list` returns **77** tools, not 80, and calling an order tool with it
answers `Unknown tool` rather than 403, because FastMCP filters by scope.

That reads exactly like a broken deploy. It isn't. Do not "fix" it by adding
`trade` to the bearer's scopes.

---

## 5. Where state lives

Everything durable is a file under the data directory: `halt_state.json`,
`circuit_breaker_state.json`, `paper_ledger.json`, `audit_chain.jsonl`,
`approval_registry_state.json`, `oauth_tokens_state.json`,
`metrics_snapshot.json`, `checkpoints.sqlite`.

**If you add a new state file, add it to `tests/conftest.py`'s
`_isolated_vesper_state` autouse fixture.** That fixture redirects every
on-disk state path to `tmp_path` for *every* test. A module that starts
touching real state mid-run silently corrupts unrelated tests later in the same
session. This has happened here before, and the symptom does not point at the
cause.

---

## 6. How to change things here

**The tests encode the decisions, not just the behaviour.** If a rule in
CLAUDE.md changes, the test is the other half of the change:

- `test_execution_guard.py` — the ticket handshake, the caps, strike-vs-premium
- `test_alerts.py` — both crossing properties (below)
- `test_notify.py` — that the ntfy topic never reaches `status()`
- `test_stream_runner.py` — catches reverting the monitor's push wake-up to a sleep
- `test_trading_mcp.py` — rule 3, mechanically

**Keep CLAUDE.md current in the same change that makes it wrong.** A stale
CLAUDE.md is worse than none, because every agent session reads it as truth.
This repo has had to spend commits correcting it after the fact
(`3c044e7`, `6a7c133`) — that's the failure mode to avoid.

**Don't commit anything showing real account numbers, balances, API keys, or an
ntfy topic.** The operator streams this work; anything checked in gets read on
stream too. An ntfy topic is a credential, not a name — there are no accounts,
so whoever knows the topic reads every alert.

---

## 7. Traps that cost an hour

The full list is [`docs/GOTCHAS.md`](GOTCHAS.md), which also covers the ones
that bite from a *different* repo in the estate. The four most likely to catch
you here:

- **The tight Webull rate limit is one bucket, not all of them.** Order query
  (where balance and positions live) is 2 req / 2s; market data is 600/min.
  `core/wb.py` handles the scarce bucket with a lock, backoff and a stale
  fallback; `core/md.py` is a separate client on the generous one. **Do not
  merge those two modules.** Route new quote reads through `core.md.Market`.
- **Buying power is shared across accounts** — totals use `max()`, not `sum()`.
- **`pip install chromadb` breaks the Webull SDK and reports success doing it.**
  It pulls `googleapis-common-protos` → `protobuf>=6`, but the Webull SDK
  requires `protobuf<6`; the conflict prints as a non-fatal `ERROR:` line that
  scrolls past `Successfully installed`. The fix is the package in the middle —
  `googleapis-common-protos<1.66`, already pinned. Nothing surfaces this at
  import time; the REST reads keep working and every `pb2` module still
  imports.
- **Never `pip install webull-openapi-mcp` into this venv.** It's a vendored
  nested checkout whose top-level `tests` package shadows this repo's `tests/`,
  and pytest then dies at collection with an error that reads like a broken
  test file rather than a packaging collision.

Two semantics that were wrong on the first pass and are now covered by tests —
don't "simplify" either:

- **Never test `price <= level` for an alert.** That fires the moment you arm
  an alert on a level price has already passed. Alerts fire on a *crossing*.
- **A moving level must never fire an alert on its own.** If the dealer-gamma
  flip moves past a stationary price, price did not break anything. Both
  previous and current price are compared against the *current* level.

---

## 8. What is not finished

Be honest about these with anyone who asks:

- **The order path has never been exercised against the live account.** It's
  tested end to end against a stub, which proves the wiring, not the broker's
  acceptance. The *payload shapes* are verified separately, via real
  non-committal `preview_order` / `preview_option` calls — that caught three
  genuine errors. First live order should be one share of something cheap,
  placed with Webull Desktop open so you can watch it land.
- **Multi-leg combos have no reachable success path in production.** Every
  legged strategy that exists today is refused at the executor, deliberately:
  the correct `option_strategy` enum is unknown beyond `SINGLE`, and a guessed
  payload to a live order endpoint is not an acceptable way to find out. The
  `GuardError` names what would make it verifiable.
- **Live-position metadata is a known gap.** Webull's position API carries no
  strategy tag and no link back to the order that created it, so the
  wheel-stock bucket, underlying-keyed swing stops and earnings-exit tagging
  work in paper mode and silently no-op on live positions.
- **`voice_tools.py` and `drafting.py` are written, tested, and not
  registered** — so `halt`, `watch_setup` and `draft_proposal` are unreachable
  on the wire while the server instructions still advertise them.
- **`vesper/bot/inbound.py`'s aiohttp webhook app exists and nothing starts
  it.** Both live approval paths are outbound-only.

---

## 9. Where to read next

| document | what it's for |
|---|---|
| [`../CLAUDE.md`](../CLAUDE.md) | **Start here.** The seven design rules and why each exists. Authoritative. |
| [`TOOLS.md`](TOOLS.md) | The 80-tool inventory, grouped by credential. |
| [`MCP_OVERVIEW.md`](MCP_OVERVIEW.md) | The external-facing explainer. Share this, not this file. |
| [`CONNECTOR_AUTH.md`](CONNECTOR_AUTH.md) | Where the token lives, why bearer ≠ OAuth here, how to reconnect the claude.ai connector when it 401s forever. |
| [`GOTCHAS.md`](GOTCHAS.md) | The estate-wide trap list. |
| [`../deploy/README.md`](../deploy/README.md) | Units, env contracts, Traefik, rollback runbook. |
| [`WEBULL_ORDER_PAYLOADS.md`](WEBULL_ORDER_PAYLOADS.md) | Verified payload shapes and the option-chain filtering gotchas. |
| [`../ROADMAP.md`](../ROADMAP.md) | Status, known gaps, ideas backlog — including rejected ideas and why. |

Docs carrying a "superseded" banner are historical records kept for the
reasoning, not current design. Trust the banner.

---

## 10. The two things most likely to be misunderstood

**Dealer gamma is a map of positioning, not a forecast.** It marks where
hedging is concentrated, which is why price often *reacts* there. It never
means price will travel there, and a wall above spot is not a reason to be
long. The failure mode is a level getting repeated back as a target. Related:
the gamma flip is a regime boundary and the two tools disagree about where it
is — one reads it off the ladder, one simulates it. When they straddle spot the
regime call is genuinely uncertain, so `td.levels()` sets `flip_split`. Do not
"fix" this by silently picking one; the disagreement is the signal.

**The LLM may narrate, reject, or shrink — never originate or increase.**
Strategies are deterministic Python. `generate_candidate_thesis()` appends
narrative *after* a proposal's numbers are fixed, so it cannot influence sizing
or entry. `audit_proposal_risk()` runs only *after* the deterministic check
passed, and may only REJECT or halve `quantity` — never approve what the
deterministic gate rejected, never increase size. It fails open on an LLM error
and is skipped entirely without an API key. This has been re-affirmed against
tempting alternatives. An LLM that can originate a position is a different
product with a different risk profile.
