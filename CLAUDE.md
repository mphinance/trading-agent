# CLAUDE.md — webull-sidecar

## What this repo is

**Vesper**, a LangGraph-based autonomous trading agent for **Webull**, plus the
market-data and signal tooling it runs on. It scans, analyses, drafts orders,
enforces risk deterministically, asks a human for approval over Telegram or
Discord, executes, and monitors open positions for exits.

It is a **single-operator personal tool**. That is a load-bearing assumption
throughout, not a packaging accident — there is no authentication, no
multi-tenancy, and no user model anywhere in this codebase.

**This app can place real orders.** See rule 3 for the one module allowed to,
and rule 1 for the network stance that makes that survivable.

Related repos, and why this isn't merged into them:
- `TraderDaddy-Desktop` — the serious desktop app (Rust/Tauri, Tradier-only,
  hard-wired). Build that one for Tradier; this one is the Webull agent.
- `traderdaddy-bridge` — a **demo** of a canonical broker schema, not a
  library. Its adapters take injected payload dicts, not credentials;
  `preview_order` echoes the request back; nothing in it can place an order;
  zero Webull. Do not try to host this there.
- `supermcp` — the live remote hub on Vultr (`ssh coolify` -> `cd supermcp`).
- `alpha-command-center` — someone else's production algo system. We don't own
  its UI. Not a home for this.

> **History that will otherwise confuse you.** Commit `de60d51` replaced an
> earlier browser-dashboard "sidecar" with Vesper and deleted 12 files in one
> go. Most got successors (`orders.py`→`vesper/execution_guard.py`,
> `risk.py`→`vesper/risk.py`, `watcher.py`→also `vesper/monitor.py`,
> `notify.py`→also `vesper/bot/*`). **`server.py`, `static/index.html` and
> `chat.py` did not, and are not coming back** — there is no HTTP API, no
> browser UI and no browser chat panel. The alert + push-stream stack
> (`alerts.py`, `quotes.py`, `notify.py`, `watcher.py`, `stream.py`) *was*
> restored on 2026-08-29 and is live again; see rules 4c and 4d. If you find a
> doc referring to a browser deck or an HTTP route, it predates all of this.

## Stack

- **Agent:** Python + LangGraph. `vesper/graph.py` compiles the node pipeline;
  `vesper/runner.py` drives a session. Disk-backed SQLite checkpointer, so a
  run paused at the approval gate survives a restart.
- **Broker:** `webull-openapi-python-sdk` 2.0.18 (pinned in `requirements.txt`)
  — `webull.core` / `webull.trade` / `webull.data`. Public.com is a second,
  partial adapter.
- **Signals/data:** TraderDaddy Pro over plain JSON-RPC (`td.py`), TickerTrace
  (`tickertrace_mcp.py`), yfinance, TradingView screener.
- **Approval channels:** Telegram (long-poll) and Discord (gateway bot).
- **LLM:** OpenRouter via `vesper/llm.py`. Narrative and red-team only — see
  rule 6.
- **MCP:** `mcp_server/` exposes the quant tooling to MCP hosts (FastMCP,
  stdio by default). `trading_mcp/` is a second, separate MCP process — an
  owner-only, READ-ONLY view over that plus Vesper's own state (account,
  halt, alerts, pending approvals, audit chain, conviction/trade-memory
  recall). Neither server holds broker credentials or an order path; see
  rule 3.
- **Trade memory:** `mcp_server/conviction.py`'s `log_conviction()` /
  `resolve_convictions()` also call `mcp_server/knowledge.py`'s
  `ingest_trade_memory()`, which embeds each entry into a ChromaDB
  `trade_memory` collection (separate from that file's book/strategy
  knowledge-base collection). `recall_similar_setups()` does the semantic
  read side — "what happened last time a setup like this one showed up" —
  and is exposed as the `recall_similar_setups` MCP tool via
  `trading_mcp/vesper_tools.py`. `chromadb` is imported at module level in
  `knowledge.py`, so an environment without it fails to import that whole
  module; every caller (including the MCP tool) catches that and degrades to
  `{"available": False, ...}` rather than crashing.
- **Deploy:** systemd **user** service (see rule 1). Remote supermcp instance is on Vultr via `ssh coolify` (`cd supermcp`).

## Critical design rules

### 1. Loopback, Tailscale, or Traefik-fronted-and-authenticated. Never bare 0.0.0.0.
This app holds live brokerage credentials and can place orders. The old form of
this rule said "nothing in this repo serves HTTP" — that was true before
Milestone 7 and is **false now**, so here is the actual posture:

`trading_mcp/server.py` runs on the coolify box as the `trading-agent.service`
systemd **user** unit, bound to the **docker bridge `10.0.0.1:8500`** — not
`0.0.0.0`, and not loopback either (Traefik is containerised and cannot reach
the host's loopback). Traefik terminates TLS at
`https://agent.mphinance.com/mcp`. **The bearer token and OAuth 2.1 are the
entire access gate**, which is why rule 2's second half exists.

`vesper/bot/inbound.py`'s aiohttp webhook app still exists and **nothing starts
it**. Both live approval paths remain **outbound-only** — Telegram long-polls,
Discord holds a gateway connection.

Two guardrails, both in code rather than in this paragraph: the server
`SystemExit`s rather than open an http listener with no token *or* a
placeholder/low-entropy one (rule 2), and `MCP_HOST` defaults to `127.0.0.1` so
reaching the bridge is always an explicit act.

### 2. Secrets live in gitignored env files — and a placeholder must never become one
**Which file is live is not obvious, and getting it wrong has already cost a
day of production exposure.** On a deploy box there are up to three candidates
and only one of them is read by a given service:

| File | Read by | Notes |
|---|---|---|
| `~/trading-agent/.env.trading-agent` | `trading-agent.service` | `EnvironmentFile=` in the unit. **This is the live one for the MCP server.** |
| `~/trading-agent/.env.vesper` | `vesper-loop.service`, `vesper-listen.service` | The agent's own contract |
| `~/trading-agent/.env` | nothing, on a deployed box | `load_dotenv()` finds it, so it silently *fills gaps* the unit's file didn't set. Editing it to rotate a credential changes nothing the service reads. |

Locally, `./.env` at the repo root is still what `vesper.py` `load_dotenv()`s at
startup. `notify.py` also still reads `../.env.notify` / `../.env.telegram`.

**Generate secrets; never copy them.** `openssl rand -hex 32`. On 2026-09-03
`trading-agent.service` served the public internet for a day with
`TRADING_AGENT_TOKEN` set to `your_strong_random_secret_token` — the literal
placeholder out of `.env.trading-agent.example`, which is committed to a
**public** repo. Everything was "working": the service was healthy, the tests
were green, TLS was valid, and an unauthenticated request was correctly refused.
The gate was simply published.

Four guardrails now, none of them a reminder:
- `core/secret_hygiene.py` — the server **refuses to open an http listener**
  with a placeholder, a low-entropy, or a hand-typed-looking token. Rejection
  reasons never quote the value (rule 5).
- `deploy/install.sh` — generates a real token when it creates the env file,
  and **refuses to install or start anything** while any credential still
  equals its `.example` value. Reports offending variable *names* only.
- `core/approval_registry.py` — a `TELEGRAM_AUTHORIZED_USER_IDS` still holding
  the example IDs authorises **nobody**. That one fails *closed*: a placeholder
  allowlist is not a weak gate, it is a working gate held by someone else.
- CI's `placeholder-hygiene` job, plus `tests/test_secret_hygiene.py`.

An `export` in a shell does **not** reach a systemd service — it gets its own
environment. Audit `git diff --cached` for `sk-ant-` / `td_live_` before any
push.

### 3. The order path is real, and it lives in exactly one file
`vesper/execution_guard.py` is the only module that can move money. Everything
else reads. If you are adding a broker write, it goes there or it does not go
in. (`orders.py`, which older docs name, is gone — this is its successor.)

Properties that hold it together, none decorative:

- **Preview, then confirm, then place.** `preview()` runs the guards and
  stages a ticket carrying a SHA-256 of the exact payload. `place()` takes a
  `ticket_id`, never an order — so no single call can both construct and fire,
  and what was approved is byte-for-byte what reaches the broker. Tickets are
  single-use and expire in 120s.
- **Guards run server-side, on every path.** Notional cap
  (`VESPER_MAX_NOTIONAL`, default $2500), quantity cap, optional symbol
  allowlist, optional buying-power fraction. A **SELL-to-open** option is
  sized off the **strike**, not the premium — a cash-secured put commits
  `strike × 100 × qty` on assignment, and reading `limit_price` instead let a
  $19k risk sail past a $2.5k cap. Multi-leg combos dispatch to a **whitelist**
  of per-strategy formulas (`_MULTI_LEG_RISK_FORMULAS`); an unregistered
  `strategy_type` is refused outright, never approximated.
- **`VESPER_TRADING=0` is the kill switch**, and it defaults **off**.
- **`vesper/halt.py` is the emergency freeze**, checked before anything else.
  `vesper/circuit_breaker.py` trips it automatically on a 15% trailing-peak NLV
  drawdown.

**Only `mcp_server` and the approval bots reach the agent; none of them holds
broker credentials or implements its own risk checks.** Any adapter that grows
its own order path is a new threat model, not a small addition.

`trading_mcp/` is an owner-only, exposure-bounded MCP surface. Under amendment A4,
the MCP surface may reach the order path, via `vesper.execution_guard` ONLY, through
a dedicated module (`trading_mcp/order_tools.py`), gated by `VESPER_TRADING`, the
deterministic notional cap, the halt state file, the circuit breaker, and an OAuth
`trade` scope distinct from `read`. Every other module under `trading_mcp/` and
`mcp_server/` remains strictly forbidden from referencing `guard.preview` or
`guard.place`. `resume()` and `ApprovalRegistry.submit_decision()` remain strictly
forbidden everywhere across all MCP modules with zero exceptions.

What did NOT change: `vesper/execution_guard.py` remains the ONLY module that can
move money, and no order execution or risk-gate code is duplicated anywhere.
`tests/test_trading_mcp.py` mechanically pins this via AST, not just a docstring promise.
That pin now also catches `getattr(guard, "place")` — dynamic dispatch by string
produces no literal attribute node, and an attribute-only walk went straight past it.

**A4 is LIVE as of 2026-09-04** (M8-24). `server.py`'s `_register_all_tools()`
now calls `register_order_tools`, so the deployed surface is 80 tools: 77 read
plus three order tools behind `require_scopes("trade")`.

Three things had to change together, and the order matters if you ever unwind it:

1. **The OAuth scope plumbing was fail-closed and had to be fixed first.**
   `SingleOperatorOAuthProvider.__init__` took one `required_scopes` argument and
   passed it as `valid_scopes` too; production calls it with `["read"]`, so the
   registerable set collapsed to `{"read"}` and no credential this server could
   issue would ever satisfy `require_scopes("trade")`. Registering the tools
   alone would have locked the owner out, not opened a hole. The constructor now
   takes `required_scopes` / `valid_scopes` / `default_scopes` as the three
   separate things they are.
2. **`trade` is in `default_scopes`, deliberately.** The claude.ai connector does
   DCR without naming a scope, so with the SDK default of `["read"]` it would
   register read-only and every order tool would answer 403. The security
   boundary is not the scope — it is the operator secret at `/authorize`, plus
   everything in the next point.
3. **The MCP notional cap was bypassable, and is now portfolio-aware.** It was
   enforced only in `place_order`, so the two-step tool path
   (`submit_manual_proposal_tool` → `place_from_ticket_tool`) reached the broker
   bounded by nothing but the guard's own, much larger cap. It is now enforced at
   the single staging chokepoint that every path shares. And it is no longer a
   flat dollar constant: the operative cap is `min(MCP_MAX_NOTIONAL,
   MCP_MAX_NOTIONAL_PCT × NLV)`, and it **fails closed** — if NLV cannot be read
   the cap is 0 and every opening order is refused, rather than falling back to
   the flat ceiling. A flat $1000 ceiling on the live $1.5k account was 2.5× its
   buying power; that is not a cap, it is decoration. Closing orders skip it
   (they cannot increase exposure). Pinned by
   `test_two_step_path_cannot_bypass_mcp_notional_cap`.

### 4. Inject live state; never make the model fetch it
`vesper/llm.py` formats portfolio + signals + dealer gamma into the turn.
Faster, no tool round-trip, guaranteed current.

The cost of injection is context, so **inject the compacted shape, never the
raw payload**. `get_gex_ticker` returns ~200 strikes / ~40KB on SPY, mostly
zeroes. `td.levels()` reduces it to ~1.3KB and is the only thing that should
be injected.

### 4a. Gamma is a map of positioning, not a forecast
Dealer gamma marks where hedging is concentrated, which is why price often
*reacts* there. It never means price will travel there, and a wall above spot
is not a reason to be long. The failure mode is a level being repeated back as
a target.

The flip is a **regime boundary**, and the two tools disagree about where it
is: `get_gex_ticker`'s `gammaFlipLevel` is read off the ladder,
`get_apex_levels`' `gammaFlip` is simulated. When they straddle spot the regime
call is genuinely uncertain, so `td.levels()` sets `flip_split`. Do not "fix"
this by silently picking one — the disagreement is the signal.

### 4b. Push accelerates the monitor; it is never a dependency
`stream.py`'s gRPC trade-event feed wakes `vesper/monitor.py` the moment an
order/position event lands, so a fill — including one placed by hand in Webull
Desktop — is acted on in ~1s instead of waiting out the 15s poll. This matters
because the monitor enforces a **-40% stop on 0DTE positions**, where a
15s-stale price is a lot of price.

If the feed can't start (no SDK, blocked egress, no credentials) the wake event
simply never fires and the loop polls exactly as before. Keep it that way: a
missing push feed must degrade, never break. The **MQTT quote feed is
deliberately not started** — nothing consumes per-tick quotes today, so it
would add a connection, a thread and a failure mode for no consumer.

### 4c. Alerts: the level is live, and a break is a transition
`alerts.py` exists because every native alert system (Webull, IBKR,
TradingView) stores a frozen NUMBER, and dealer gamma moves daily. An alert
here can reference `flip` / `pin` / `wall_above` / `wall_below`, re-resolved
from TDPro every tick. Armed with `vesper.py alerts --arm SYMBOL LEVEL
DIRECTION`; **evaluated by the watcher thread inside `vesper loop`** — a
one-shot CLI process cannot watch anything after it exits.

Do not "simplify" either of these; both were wrong on the first pass and both
are covered by tests:

- **Never test `price <= level`.** That fires the moment you arm an alert on a
  level price has already passed. Alerts fire on a CROSSING, and one armed on
  the wrong side starts `pending` until price returns.
- **A moving level must never fire an alert on its own.** If the flip moves
  past a stationary price, price did not break anything. Both previous and
  current price are compared against the CURRENT level.

`resolve_level()` returns None rather than falling back to a remembered number
when TDPro is unavailable. A stale flip is the exact failure this module exists
to prevent, so an outage silences dynamic alerts instead of misfiring them.

The watcher is a background **thread**, not an asyncio task — the Webull SDK is
blocking, and a slow snapshot inside the event loop would stall the graph and
the Telegram poller.

Delivery is `notify.Notifier` → ntfy and/or Telegram. **An ntfy topic is a
credential, not a name.** There are no accounts: whoever knows the topic reads
every alert. So it is minted with 128 bits of randomness and `status()` must
never return it. If you add a channel, keep its secret out of `status()` the
same way.

### 4d. Voice is claude.ai over the MCP connector, and it never confirms an order
**The Telegram-voice-note design that used to be this rule is CANCELLED**
(reversed 2026-09-01). No STT, no TTS, no audio handling and no audio endpoint
belongs in this repo — not one of them. If you find a doc describing Whisper /
Voxtral / Kokoro or `sendVoice`, it predates the reversal; `docs/VOICE_STACK_GUIDE.md`
is now just a superseded notice.

Voice is: talk to Claude on the phone, Claude calls the read-only MCP connector
at `agent.mphinance.com`. That deletes an entire subsystem rather than building
one, and claude.ai voice mode already works with connectors on mobile.

**The one constraint that survives the reversal, unchanged: voice asks
questions; buttons move money.** Approvals must never become a voice command.
A transcript is ambiguous in exactly the wrong place ("approve" — *which*
proposal?), while an inline Telegram/Discord button is unambiguous, per-user
authorised and restart-safe. `resume()` and `submit_decision()` are forbidden
to every MCP tool for this reason (rule 3), not merely by convention.

The two measured failure modes still shape tool design: ticker
mis-transcription (NVDA → "in video"), so aim tools at questions about
*current* context — P&L, the flip, what's open, what's pending — rather than
at naming a symbol; and payloads too large to read aloud, so keep tool output
compact (rule 4's injection discipline applies here too).

### 5. Assume this is on video
The user streams this work. Anything checked in gets read on stream too — do
not commit a screenshot, log or fixture showing real account numbers,
balances, API keys or an ntfy topic. (The old browser deck did live regex
scrubbing of `sk-ant-…` / `td_live_…` before rendering; with the UI gone, the
protection is now entirely "don't commit it" plus CI's credential scan.)

### 6. The LLM may narrate, reject, or shrink — never originate or increase
`generate_candidate_thesis()` appends narrative *after* a proposal's numbers
are already fixed, so it cannot influence sizing or entry.
`audit_proposal_risk()` runs in `risk_gate_node` only *after* the
deterministic check passed, and may only REJECT or halve `quantity` — never
approve what the deterministic gate rejected, never increase size. It fails
open (skips) on an LLM error and is skipped entirely without an API key.

This is deliberate and has been re-affirmed against tempting alternatives
(see ROADMAP's rejected-ideas notes). Strategies are deterministic Python.
An LLM that can originate a position is a different product with a different
risk profile.

### 7. Prefer the subscription credential
`CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) over
`ANTHROPIC_API_KEY`. On the OAuth token there is no per-token cost, so **model
choice is free** — don't reach for a weaker model to "save money" that isn't
being spent.

## Gotchas that will cost you an hour

> **See also [`docs/GOTCHAS.md`](docs/GOTCHAS.md)** — the estate-wide list
> (TMpro backend, Vespryx, supermcp, infrastructure). The ones below are
> specific to this repo; that file covers the ones that bite from a *different*
> repo than the one you are editing, which is where the expensive hours went.


- **The tight rate limit is one bucket, not all of them.** US region: order
  query (where balance and positions live) is **2 req / 2s**, but market data
  is **600 req/min** and order place/replace/cancel is 600/min. `wb.py` uses a
  lock, backoff and a stale fallback for the scarce bucket; `md.py` is a
  separate client on the generous one. **Do not merge those two modules**, and
  route new quote reads through `md.Market` rather than the raw SDK client so
  they inherit its chunking and caching.
- **Buying power is shared across accounts** — totals use `max()`, not `sum()`.
- **`sk-ant-oat…` is an OAuth token, not an API key.** Same prefix, same ~108
  length, so every structural check passes — but in `ANTHROPIC_API_KEY` it
  yields `401 invalid x-api-key`.
- **TDPro's `get_conviction` takes `symbol`, not `ticker`** — an unknown key is
  silently ignored and you get the market-wide gauge for every call.
  **`get_earnings_flow` has the same trap**: its `symbol` argument is not a
  filter at all, it always returns the full market-wide slate.
- **TDPro doesn't declare a charset**, so `requests` decodes UTF-8 as
  ISO-8859-1 and em-dashes arrive as `â€"`. `td.py` pins it.
- **Python 3.14 works.** SDK 2.0.18 declares `python_requires='>=3.8,<3.15'`
  with explicit cryptography/grpcio pins for 3.14.

## Layout

```
vesper.py          CLI entrypoint: scan / analyze / 0dte / morning / monitor /
                   loop / listen / alerts / halt / resume / status / paper
vesper/
  graph.py         LangGraph pipeline + disk-backed SQLite checkpointer
  runner.py        Drives one agent session
  loop.py          Unattended daemon: scheduled scans + monitor + alert watcher
  state.py         Pydantic models (OrderProposal, OrderLeg, TradingState, …)
  execution_guard.py  THE ORDER PATH — guards, tickets, multi-leg risk formulas
  risk.py          RiskEnforcer: sizing + capital-allocation buckets
  circuit_breaker.py  Trailing-peak NLV drawdown -> automatic halt
  halt.py          Emergency freeze, checked by the guard before anything else
  monitor.py       Position monitor + exit cascade (push-woken, see rule 4b)
  paper_ledger.py  Simulated fills, mark-to-market, realized/unrealized P&L
  account.py       Live equity/NLV reads
  sector.py        Ticker -> sector (yfinance), for the concentration bucket
  llm.py           OpenRouter: thesis narrative + risk red-team (rule 6)
  flow_classifier.py  Directional-vs-hedge options flow scoring
  alerts_runner.py    Builds/starts the alert watcher (rule 4c)
  stream_runner.py    gRPC trade-event push -> monitor wake-up (rule 4b)
  nodes/           regime, scanner, analyst, playbooks, risk_gate,
                   human_gate, executor, reflection
  bot/             Telegram + Discord adapters, gateway, inbound approvals
  brokers/         public_broker.py (second, partial adapter)

core/              Shared layer both vesper/ and trading_mcp/ import from (the
                   M0 split). secret_hygiene.py lives here because BOTH need
                   it — it refuses placeholder credentials (rule 2).
wb.py              Webull client — credentials, account/position/order reads,
                   caching and the scarce 2-req/2s bucket
md.py              Market data, research, screeners, watchlists (600/min bucket)
td.py              TraderDaddy Pro client + td.levels() dealer-gamma compaction
edgar.py           SEC EDGAR client — filings, XBRL financials, shares
                   outstanding, AS-FILER 13D/13G stakes. Needs SEC_USER_AGENT.
alerts.py          Alert store + crossing logic (a level can BE dealer structure)
watcher.py         Background thread evaluating alerts
quotes.py          Last price w/ fallback chain: md snapshot -> portfolio -> TDPro
notify.py          Alert delivery: ntfy and/or Telegram
stream.py          MQTT quote push + gRPC trade-event push onto one bus
tickertrace_mcp.py / momentum_mcp.py   Data-source MCP clients
mcp_server/        Quant tooling exposed over MCP (FastMCP, stdio). registry.py's
                   register_momentum_tools() registers tiers 1-3 (47 tools) onto
                   any FastMCP instance in-process — used by both server.py here
                   and trading_mcp/server.py below. conviction.py is the
                   conviction journal; knowledge.py is the Chroma-backed
                   knowledge base AND the trade-memory layer — see below.
trading_mcp/       PHASE 0: owner-only, READ-ONLY MCP server (separate process
                   from mcp_server/server.py and from supermcp). server.py wires
                   registry.py's tiers 1-3 plus vesper_tools.py's 13 read-only
                   Vesper tools (account/halt/drawdown/paper/alerts/pending-
                   approvals/audit-chain/playbook-calibration/trade-memory-recall/
                   position-monitor-preview) onto one FastMCP("trading-agent").
                   Auth is HmacStaticTokenVerifier off TRADING_AGENT_TOKEN AND
                   an OAuth 2.1 provider (oauth_provider.py) with dynamic client
                   registration, wrapped in one MultiAuth so both converge on a
                   single verify_token; stdio (the default) needs none. In
                   production this is NOT loopback: it binds the docker bridge
                   10.0.0.1:8500 behind Traefik at agent.mphinance.com (rule 1),
                   and refuses to start with a missing OR placeholder token
                   (core/secret_hygiene.py, rule 2).
                   order_tools.py IS registered as of 2026-09-04 (M8-24): three
                   order tools behind require_scopes("trade"), 80 tools total.
                   This server is NO LONGER read-only. voice_tools.py and
                   drafting.py remain written, tested and NOT registered. No
                   non-order tool here may call
                   guard.preview()/guard.place(), resume(), or
                   ApprovalRegistry.submit_decision() — see rule 3's note above,
                   app_spec.txt's A3, and tests/test_trading_mcp.py's AST-based
                   pin.
tests/             pytest, hermetic — Webull and Agent SDKs stubbed in conftest
deploy/            LIVE (M7): three systemd user units, two env contracts,
                   Traefik dynamic config, idempotent install.sh that generates
                   a real token and refuses to deploy a placeholder. See
                   deploy/README.md.
docs/              API.md, expansion plan, OpenRouter pricing, voice stack
ROADMAP.md         Single planning doc: status, known gaps, ideas backlog
```

## Tests

`pip install -r requirements-dev.txt && pytest -q`. **815 passing.** The suite
is hermetic — no network, no broker, no credentials — because a green build
must not depend on TDPro or ntfy.sh being up.

- **`requirements-dev.txt` is not a superset of `requirements.txt`.** The
  Webull SDK and Agent SDK are stubbed in `tests/conftest.py`: one needs a
  compiler and pins the Python version, the other shells out to an npm-only
  binary. Do not "fix" this by installing the real ones. It *does* need
  transitive deps that no test imports directly (`tradingview_screener`,
  `fastmcp`) — verify with a clean-venv `pip install -r requirements-dev.txt &&
  pytest --collect-only`, which is the only check that catches those.
- **`conftest.py`'s `_isolated_vesper_state` autouse fixture** redirects every
  on-disk state file (halt, circuit breaker, paper ledger, approval registry,
  graph checkpoints) to `tmp_path` for *every* test. Add any new state file to
  it — a module that starts touching real state mid-run silently corrupts
  unrelated tests later in the same session, which has happened here before.
- **The tests encode the decisions in the rules above, not just behaviour.**
  `test_execution_guard.py` pins the ticket handshake, the caps and the
  strike-vs-premium rule; `test_alerts.py` pins both crossing properties;
  `test_notify.py` asserts the ntfy topic never reaches `status()`;
  `test_stream_runner.py` catches reverting the monitor's push wake-up back to
  a plain sleep; `test_trading_mcp.py` pins rule 3 mechanically, so a new MCP
  tool that quietly grows its own order path fails a test, not just a review.
  If a rule here changes, the test is the other half of the change.
- **That rule-3 pin matches attribute *access*, not just invocation, and it
  has to.** The live order path passes the bound method rather than calling
  it inline — `asyncio.to_thread(guard.place, …)` at `executor.py:262` and
  `monitor.py:404`. The first version of this test only matched AST `Call`
  nodes shaped `guard.place(...)`, so a new tool copying that `to_thread`
  idiom — the one it would most plausibly copy, since it's what the existing
  path does — would have added a working order path and still passed. It now
  resolves import aliases (`import guard as g`) and dotted access
  (`vesper.execution_guard.guard.place`) too, and
  `test_guard_pin_catches_indirect_call_shapes` asserts each of those four
  shapes is caught. Don't narrow it back to `ast.Call`.
- **Faking `vesper/llm.py`'s OpenRouter calls has one pattern, not several.**
  `tests/llm_fakes.py`'s `DeterministicProvider` is a small async-callable
  test double matching `call_openrouter`'s signature, backed by a response
  queue and recording `.calls`; wire it in with
  `monkeypatch.setattr("vesper.llm.call_openrouter", provider)`, which reaches
  both `generate_candidate_thesis` and `audit_proposal_risk` because they call
  the module-level name. It's imported as `from llm_fakes import
  DeterministicProvider` (not `tests.llm_fakes`) — `webull-openapi-mcp/tests/`
  already claims the `tests` package name, and pytest's rootless import mode
  puts this repo's `tests/` directly on `sys.path` anyway. Separately,
  `patch("vesper.llm.is_llm_enabled", return_value=False)` is the standing
  pattern for keeping the LLM out of a test entirely (portfolio-governance,
  sector-concentration, execution-integration tests) — untouched, still
  correct, don't replace it with provider injection.

## Status

**Verified against the live account:** positions, balances, TDPro signals,
dealer-gamma levels, market-data snapshots (`md.Market.snapshot` confirmed
returning live prices), TDPro's earnings calendar and unusual-activity feed.

**NOT exercised against the live account: the order path.** It is tested end
to end against a stub broker — payload shapes, guards, ticket lifecycle,
multi-leg combos — which proves the wiring, not the broker's acceptance of it.
Webull's fractional-share and extended-hours rules are unproven here.
**First live order should be one share of something cheap, placed with Webull
Desktop open so you can watch it land.**

**The payload shapes themselves are now verified** (2026-08-29) by sending real
`preview_order` / `preview_option` calls — preview is non-committal, so this
was established without placing an order. See
[`docs/WEBULL_ORDER_PAYLOADS.md`](docs/WEBULL_ORDER_PAYLOADS.md) for both
shapes and the option-chain filtering gotchas. That fixed three real errors in
the single-leg path (wrong signature, `asset_type` instead of
`instrument_type`, five missing required fields) and the multi-leg path's leg
identification (a leg is keyed by **underlying + strike + expiry + option_type**,
not a contract symbol; `side` is required at both order and leg level).

**Combos beyond a single option leg are refused at the executor**, deliberately:
`option_strategy` only has `SINGLE` confirmed, so SYNTHETIC_LONG's correct enum
is unknown, and whether `place_option` accepts THEGA's equity leg at all is
unverified. Both raise a `GuardError` naming what would make them verifiable
rather than sending a guessed payload to a live order endpoint. Consequence
worth knowing: `_execute_webull_multileg` currently has **no reachable success
path in production** — every legged strategy that exists today is refused.

**Live-position metadata is a known gap.** Webull's position API carries no
strategy tag and no link back to the order that created it, so three features
(wheel-stock bucket, underlying-keyed swing stops, earnings-exit tagging) work
in paper mode and silently no-op on live positions. ROADMAP explains what to
verify with live access before building the registry that would close it.

**Deployment:** Managed under `deploy/` with three systemd user units (`trading-agent.service`,
`vesper-loop.service`, `vesper-listen.service`) and two env contracts (`.env.trading-agent`,
`.env.vesper`). See `deploy/README.md`.

**Live as of 2026-09-03**, on the coolify box (`ssh coolify`):

| | |
|---|---|
| `trading-agent.service` | **running**, lingering on. 80 tools — 77 read + 3 order. |
| Reachable at | `https://agent.mphinance.com/mcp` — Traefik, LE cert to 2026-12-01 |
| Binds | `10.0.0.1:8500` (docker bridge — rule 1) |
| Auth | bearer + OAuth 2.1, converged in one `MultiAuth` |
| Credentials on box | live Webull **prod**, TDPro, EDGAR |
| `vesper-loop` / `vesper-listen` | enabled but **stopped** — the agent itself does not run there yet |
| `VESPER_TRADING` | **1** in `.env.trading-agent`, at the operator's instruction 2026-09-04. |
| MCP order caps | `MCP_MAX_NOTIONAL_PCT=0.25` of NLV · `MCP_MAX_NOTIONAL=1000` ceiling · 5 orders/day |

So the box **can place an order** as of 2026-09-04. What still bounds it: the
portfolio-aware notional cap (~$378 on a $1.5k book — the pct term binds, not
the $1000 ceiling), `MCP_MAX_DAILY_ORDERS=5`, the guard's own $2500 cap, the
halt file and the circuit breaker. Approval still cannot be given by a tool —
`resume()` and `submit_decision()` remain unreachable from every MCP module,
so voice/chat can *originate* an order but never *approve* a pending one.

**Note on the env contract.** `VESPER_TRADING` now lives in
`.env.trading-agent`, which `deploy/README.md` previously said it never should.
That separation was written when this server was read-only; arming the MCP
order path requires the variable in the file the service actually reads,
because `trading-agent.service` does not read `.env.vesper`. Both files now set
it and they must be kept in step — a divergence means the MCP surface and the
agent loop disagree about whether trading is live.
