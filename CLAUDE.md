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
- **Broker:** `webull-openapi-python-sdk` 2.0.16 — `webull.core` /
  `webull.trade` / `webull.data`. Public.com is a second, partial adapter.
- **Signals/data:** TraderDaddy Pro over plain JSON-RPC (`td.py`), TickerTrace
  (`tickertrace_mcp.py`), yfinance, TradingView screener.
- **Approval channels:** Telegram (long-poll) and Discord (gateway bot).
- **LLM:** OpenRouter via `vesper/llm.py`. Narrative and red-team only — see
  rule 6.
- **MCP:** `mcp_server/` exposes the quant tooling to MCP hosts (FastMCP,
  stdio by default).
- **Deploy:** systemd **user** service (see rule 1).

## Critical design rules

### 1. Loopback or Tailscale. Never 0.0.0.0.
This app has **no authentication**, holds live brokerage credentials, and can
place orders. `deploy/install.sh` *refuses to run* without a Tailscale IP
rather than falling back — the guardrail is in code, not in a comment.

Nuance since the migration: **nothing in this repo currently serves HTTP.**
The only HTTP server left is `vesper/bot/inbound.py`'s aiohttp webhook app,
and nothing starts it. Both live approval paths are **outbound-only** —
Telegram long-polls, Discord holds a gateway connection — so no inbound port
is opened at all today. That is a stronger position than the rule requires;
the rule exists for the moment someone re-adds a listener. `SIDECAR_HOST` and
`deploy/sidecar.service` still assume the old `run.sh`/port-8787 layout and
are **stale** — treat them as unverified until someone re-deploys.

### 2. Secrets live in gitignored env files, in two places
- **`./.env`** at the repo root — where this project's credentials actually
  live now (`TD_API_KEY`, `TDPRO_API_KEY`, `WEBULL_APP_KEY`,
  `WEBULL_APP_SECRET`, `WEBULL_KEY`, `WEBULL_SECRET`, `WEBULL_REGION_ID`,
  `WEBULL_ENVIRONMENT`). Gitignored, 0600. `vesper.py` `load_dotenv()`s it at
  startup, which is why every module can read `os.environ`.
- **`../.env.*`** one directory up — the original convention (uncommittable by
  construction). `notify.py` still reads `../.env.notify` / `../.env.telegram`
  *and* `./.env`, so either works.

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
- **Python 3.14 works.** SDK 2.0.16 declares `python_requires='>=3.8,<3.15'`
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

wb.py              Webull client — credentials, account/position/order reads,
                   caching and the scarce 2-req/2s bucket
md.py              Market data, research, screeners, watchlists (600/min bucket)
td.py              TraderDaddy Pro client + td.levels() dealer-gamma compaction
alerts.py          Alert store + crossing logic (a level can BE dealer structure)
watcher.py         Background thread evaluating alerts
quotes.py          Last price w/ fallback chain: md snapshot -> portfolio -> TDPro
notify.py          Alert delivery: ntfy and/or Telegram
stream.py          MQTT quote push + gRPC trade-event push onto one bus
tickertrace_mcp.py / momentum_mcp.py   Data-source MCP clients
mcp_server/        Quant tooling exposed over MCP (FastMCP, stdio)
tests/             pytest, hermetic — Webull and Agent SDKs stubbed in conftest
deploy/            systemd unit + Tailscale-gated installer (STALE, see rule 1)
docs/              API.md, expansion plan, OpenRouter pricing, voice stack
ROADMAP.md         Single planning doc: status, known gaps, ideas backlog
```

## Tests

`pip install -r requirements-dev.txt && pytest -q`. **384 passing.** The suite
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
  a plain sleep. If a rule here changes, the test is the other half of the
  change.

## Status

**Verified against the live account:** positions, balances, TDPro signals,
dealer-gamma levels, market-data snapshots (`md.Market.snapshot` confirmed
returning live prices), TDPro's earnings calendar and unusual-activity feed.

**NOT exercised against the live account: the order path.** It is tested end
to end against a stub broker — payload shapes, guards, ticket lifecycle,
multi-leg combos — which proves the wiring, not the broker's acceptance of it.
Webull's field-level validation, fractional-share and extended-hours rules, and
option-strategy handling are unproven here. **First live order should be one
share of something cheap, placed with Webull Desktop open so you can watch it
land.** There is also a known signature mismatch in the single-leg live path
(`place_order(payload)` vs the SDK's `place_order(account_id, new_orders, …)`)
that is flagged in ROADMAP and deliberately not fixed blind.

**Live-position metadata is a known gap.** Webull's position API carries no
strategy tag and no link back to the order that created it, so three features
(wheel-stock bucket, underlying-keyed swing stops, earnings-exit tagging) work
in paper mode and silently no-op on live positions. ROADMAP explains what to
verify with live access before building the registry that would close it.

**Deployment is stale.** `deploy/` still describes the pre-migration
`run.sh`/port-8787 service. Nothing redeploys automatically. Re-verify before
trusting anything about a running instance.
