# CLAUDE.md — webull-sidecar

## What this repo is

A companion deck for **Webull Desktop**: live positions, portfolio guardrails,
TraderDaddy Pro signals, dealer-gamma structure, live price alerts, a Claude chat
panel, and an **MCP server that lets Claude Desktop read the account and trade it
by voice**. Runs beside the Webull app, not on top of it.

The intended rig is three windows: **Webull Desktop** (charts, manual trading),
**sidecar** (the deck), and **Claude Desktop** (voice, connected to sidecar's
MCP server). You talk to Claude; Claude calls sidecar; sidecar calls Webull.

**This app can place orders.** That was not true before — see rule 3 for what
changed and what guards it. See [README.md](./README.md) for setup, deploy, and
the full list of API traps, and [docs/API.md](./docs/API.md) for every MCP tool,
HTTP route, the ticket handshake, and the SSE event shapes.

**This is the org's only real Webull integration.** The one in `trading-dashboard`
(now archived) was a credential form wired to a `setTimeout`. Nothing else here
talks to Webull.

Related repos, and why this isn't merged into them:
- `TraderDaddy-Desktop` — the serious desktop app (Rust/Tauri, Tradier-only,
  hard-wired). Build that one for Tradier; this one is the Webull companion.
- `traderdaddy-bridge` — a **demo** of a canonical broker schema, not a library.
  Its adapters take injected payload dicts, not credentials; `preview_order`
  echoes the request back; nothing in it can place an order; zero Webull. Do not
  try to host this there.
- `alpha-command-center` — someone else's production algo system. We don't own
  its UI. Not a home for this.

## Stack

- **Backend:** Python + FastAPI + uvicorn. No build step, no frontend framework.
- **Broker:** `webull-openapi-python-sdk` 2.0.16 (the official one) —
  `webull.core` / `webull.trade` / `webull.data`. Accounts, balances, positions,
  market data, options, research, screeners, watchlists, and the full v3 order
  surface.
- **Push:** MQTT (`paho`, via `DataStreamingClient`) for quotes, gRPC (via
  `TradeEventsClient`) for order/position/fill events — so a fill you make in
  Webull Desktop shows up here in about a second instead of on the next poll.
- **Voice:** `mcp_server.py` on the official `mcp` SDK (`MCPServer`), stdio
  transport, for Claude Desktop. Browser-side mic is separate — see rule 4c.
- **Signals:** TraderDaddy Pro over plain JSON-RPC (`POST /api/v1/mcp`). No MCP
  client library — the endpoint takes a bare `tools/call` with no handshake.
  (Unrelated to our own MCP server, despite the name collision.)
- **Chat:** Claude Agent SDK (`claude_agent_sdk`), which shells out to the
  `claude` CLI binary.
- **UI:** one `static/index.html`. Inline SVG/CSS, no deps, no bundler.
- **Deploy:** systemd **user** service bound to a Tailscale IP (see rule 1).

## Critical design rules

### 1. Loopback or Tailscale. Never 0.0.0.0.
This app has **no authentication**, holds live brokerage credentials, and can
place orders. Binding it to a LAN lets any device on the wifi read the account's
balances and positions *and trade with them*. `deploy/install.sh` *refuses to
run* without a Tailscale IP rather than falling back — the guardrail is in code,
not in a comment. Live on venus at `100.113.21.73:8787`, tailnet-only.

This rule got more load-bearing when rule 3 changed. `SIDECAR_TRADING=0` is the
lever if you ever need the deck somewhere less private.

### 2. Secrets live outside the repo, and only in files
`../.env.webull`, `../.env.anthropic` and `../.env.telegram` sit in the **parent**
directory, so they cannot be committed by construction. `run.sh` sources them. An `export` in a
shell does **not** reach the server — every Bash call spawns a fresh shell, and
systemd gets its own environment. Audit `git diff --cached` for `sk-ant-` /
`td_live_` before any push.

### 3. The order path is real, and it lives in exactly one file
This rule used to read "there is no order path, keep it that way." That was
reversed deliberately, on request, with the threat model the old rule asked for.
`orders.py` is now the only module that can move money. Everything else reads.
If you are adding a broker write, it goes there or it does not go in.

Three properties hold it together — none is decoration:

- **Preview, then confirm, then place.** `preview()` runs the guards, gets
  Webull's own cost estimate, and stages a ticket carrying a SHA-256 of the
  exact payload. `place()` takes a `ticket_id`, never an order. So no single
  call can both construct and fire, and what gets confirmed out loud is
  byte-for-byte what reaches the broker. Tickets are single-use and expire in
  120s. `SIDECAR_ORDER_CONFIRM=0` collapses this to one step.
- **Guards run server-side, on every path.** Notional cap
  (`SIDECAR_MAX_NOTIONAL`, default $2500), quantity cap, optional symbol
  allowlist, optional buying-power fraction. `replace` re-runs them, because
  amending a working order can raise exposure. `cancel` never runs them —
  reducing risk is always allowed. Market orders get priced from the live quote
  so the cap can't be dodged by omitting a limit.
- **`SIDECAR_TRADING=0` is the kill switch.**

**The in-app chat panel has no order tools, and that is deliberate.** `chat.py`
holds `WebFetch`/`WebSearch`, so it reads text written by strangers. Giving
order tools to a component with attacker-controllable input makes the account
the payload of any prompt injection. Claude Desktop over MCP is different: it is
driven by the user's voice, not by a page it fetched. If you ever wire the panel
to `orders.py`, that is a new threat model, not a small addition.

This also raises the stakes on rule 1. An unauthenticated read-only deck on a
tailnet is survivable; an unauthenticated *trading* deck on 0.0.0.0 is not.

### 4. Inject live state into chat; never make the model fetch it
`WebFetch` upgrades `http://` to `https://`, so it **cannot** read this app's own
loopback server. `chat.py` formats the portfolio + signals + dealer gamma into the
turn instead. Faster, no tool round-trip, guaranteed current.

The cost of injection is context, so **inject the compacted shape, never the raw
payload**. `get_gex_ticker` returns the full strike ladder — ~200 strikes, ~40KB
on SPY, mostly zeroes. `td.levels()` reduces it to ~1.3KB and is the only thing
`_fmt_levels()` will read. If a future block starts costing more than the
portfolio it is meant to inform, compact it before injecting it.

### 4b. Gamma is a map of positioning, not a forecast
Dealer gamma marks where hedging is concentrated, which is why price often
*reacts* there. It never means price will travel there, and a wall above spot is
not a reason to be long. The system prompt says this explicitly and the read
should keep saying it — the failure mode is a level being repeated back as a
target.

The flip is a **regime boundary**, and the two tools disagree about where it is:
`get_gex_ticker`'s `gammaFlipLevel` is read off the ladder, `get_apex_levels`'
`gammaFlip` is simulated. When they straddle spot the regime call is genuinely
uncertain, so `td.levels()` sets `flip_split` and both the UI and the prompt say
so. Do not "fix" this by picking one silently — the disagreement is the signal.

### 4c. Voice is browser-side only
The mic uses the Web Speech API in `static/index.html`. Nothing about voice
reaches the server — the transcript arrives as an ordinary chat POST. Keep it
that way; an audio upload path would be a new threat model for a panel that
already holds brokerage credentials and no authentication.

Two traps that will look like bugs: recognition transcribes the app's own
spoken reply if synthesis isn't cancelled first, and tickers are mis-transcribed
constantly (NVDA becomes "in video"). The focused Dealer Gamma symbol is sent
with every turn so the common questions need no ticker at all.

### 4d. Alerts: the level is live, and a break is a transition
`alerts.py` exists because every native alert system (Webull, IBKR, TradingView)
stores a frozen NUMBER, and dealer gamma moves daily. An alert here can reference
`flip` / `pin` / `wall_above` / `wall_below`, re-resolved from TDPro every tick.

Do not "simplify" either of these; both were wrong on the first pass and both are
covered by tests:

- **Never test `price <= level`.** That fires the moment you arm an alert on a
  level price has already passed. Alerts fire on a CROSSING, and one armed on the
  wrong side starts `pending` until price returns.
- **A moving level must never fire an alert on its own.** If the flip moves past
  a stationary price, price did not break anything. Both previous and current
  price are compared against the CURRENT level, so only price movement can cross
  it; a level that jumps over price re-pends the alert instead of firing.

Also: `resolve_level()` returns None rather than falling back to a remembered
number when TDPro is unavailable. A stale flip is the exact failure this module
exists to prevent, so an outage silences dynamic alerts instead of misfiring them.

The watcher is a background THREAD, not an asyncio task — the Webull SDK is
blocking, and a slow snapshot inside the event loop would stall the SSE chat
stream that shares it.

Delivery is `notify.Notifier`, fanning out to ntfy and/or Telegram. **An ntfy
topic is a credential, not a name.** There are no accounts: whoever knows the
topic reads every alert. So it is minted with 128 bits of randomness, the env
file is 0600, and `status()` must never return it — this panel is streamed, and
a topic read off a frame is a subscription someone else keeps. If you add a
channel, keep its secret out of `status()` the same way.

### 4e. MCP is the control surface, never the watcher
`mcp_server.py` connects Claude Desktop over stdio and is a thin client: no
credentials, no broker, one HTTP call per tool to routes that already exist.
sidecar stays the only process holding the Webull keys.

That thinness is also what makes the order tools safe to expose here: the
notional cap, the allowlist and the preview→confirm handshake live in
`orders.py` and apply to voice trades for free. An MCP server that built its own
Webull client would need two 2FA token files, would race the deck for the same
2 req/2s account budget, and would drift from the deck's guards within a week.
Don't. Use `mcp.sh`, which is a stdio wrapper over the same HTTP API.

A stdio MCP server only runs while Claude Desktop is talking to it, so nothing
that must happen while you are away can live there. Alerts are evaluated by
sidecar's own thread; MCP only arms and inspects.

Keep it stdio. A remote connector means exposing an app with no authentication
to the internet — supermcp is where a shareable, OAuth-gated version belongs.

### 5. Assume this is on video
The user streams this panel. `static/index.html` scrubs anything matching
`sk-ant-…` / `td_live_…` before rendering, and `chat.py` sets
`setting_sources=[]` so the user's `~/.claude` config never leaks into frame.
Prompts are not a guarantee; the scrub is. The same logic applies to anything
checked into the repo, not just what renders live — screenshots and docs get
read on stream too; don't commit one showing real account numbers or balances.

### 6. Prefer the subscription credential
`CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) over `ANTHROPIC_API_KEY`.
On the OAuth token there is no per-token cost, so **model choice is free** —
default `claude-sonnet-5`, and don't reach for Haiku to "save money" that isn't
being spent. The SDK still reports `total_cost_usd` on OAuth; it's notional.
Note: the Agent SDK docs say offering claude.ai login *in a product for other
users* needs prior approval — personal use only.

## Gotchas that will cost you an hour

All verified 2026-07-16 and documented at length in the README:

- **The tight rate limit is one bucket, not all of them.** US region:
  order query (which is where balance and positions live) is **2 req / 2s**,
  but **market data is 600 req/min** and order place/replace/cancel is
  600 req/min. `wb.py` uses a lock, backoff and a stale fallback for the scarce
  bucket; `md.py` is a separate client on the generous one, which is why live
  quotes can refresh every second without starving the portfolio poll. Do not
  merge those two modules.
- **Buying power is shared across accounts** — totals use `max()`, not `sum()`.
- **`sk-ant-oat…` is an OAuth token, not an API key.** Same prefix, same ~108
  length as a real key, so every structural check passes — but it belongs in
  `CLAUDE_CODE_OAUTH_TOKEN`, and in `ANTHROPIC_API_KEY` it yields
  `401 invalid x-api-key`.
- **The Agent SDK reports auth failure as** `Claude Code returned an error
  result: success`. Useless. Test the credential directly against the API.
- **TDPro's `get_conviction` takes `symbol`, not `ticker`** — an unknown key is
  ignored and you silently get the market-wide gauge for every call.
- **TDPro doesn't declare a charset**, so `requests` decodes UTF-8 as
  ISO-8859-1 and em-dashes arrive as `â€"`.
- **Python 3.14 now works.** This used to be `>=3.8,<3.14`; SDK 2.0.16 declares
  `python_requires='>=3.8,<3.15'` with explicit cryptography/grpcio pins for
  3.14. venus no longer needs its `python3.10` venv — though the existing one is
  fine and there's no reason to rebuild it mid-session.
- **The Python Agent SDK ships no `claude` binary** (only the TS one does).
  `npm i -g @anthropic-ai/claude-code` or chat fails at runtime, not install.

## Layout

```
wb.py          Webull client — credentials, account/balance/position/order reads,
               caching and rate-limit handling. Owns the shared ApiClient.
md.py          Market data, research, screeners, watchlists (600/min bucket)
orders.py      THE ORDER PATH — guards, preview/confirm tickets, place/replace/cancel
stream.py      MQTT quote push + gRPC trade-event push, bridged onto an SSE bus
risk.py        Portfolio guardrails
td.py          TraderDaddy Pro client (direct JSON-RPC) + dealer-gamma levels
chat.py        In-app Claude chat via the Agent SDK. Read-only tools, no order path.
alerts.py      Alert store + crossing logic (a level can BE the dealer structure)
quotes.py      Last price: Webull data -> portfolio -> TDPro spot, tagged by source
watcher.py     Background thread that evaluates alerts and delivers them
notify.py      Alert delivery: ntfy (no signup) and/or Telegram
mcp_server.py  Claude Desktop MCP server (stdio, thin client over the HTTP API)
mcp.sh         What Claude Desktop spawns
server.py      FastAPI routes
static/        Single-page UI, no build step
deploy/        systemd unit + Tailscale-bound installer
tests/         pytest suite, hermetic — the Webull and Agent SDKs are stubbed
               in conftest.py so CI needs neither a compiler nor a broker
.github/       CI on Python 3.10 and 3.14, a compileall pass, the UI check with
               node present, and a credential scan. Every push and PR.
docs/API.md    Generated-from-code reference: MCP tools, HTTP routes, SSE event
               shapes, the ticket handshake. Regenerate it if you add a route or
               a tool — a stale API doc is worse than none.
```

Two modules read prices and they are not redundant. `md.py` is the full market
data surface (quotes, depth, bars, chains, research) and reports failure.
`quotes.py` is a last-price cache with a fallback chain — Webull snapshot,
then portfolio, then TDPro spot — so the alert watcher keeps working when market
data is unentitled. Substituting a source is right for an alert and wrong for
research; that's why they're separate.

## Tests

`pip install -r requirements-dev.txt && pytest -q`. CI runs the same on every
push and PR. The suite is hermetic — no network, no broker, no credentials —
because a green build must not depend on TDPro or ntfy.sh being up.

Two rules if you touch it:

- **`requirements-dev.txt` is not a superset of `requirements.txt`.** The Webull
  SDK and the Agent SDK are stubbed in `tests/conftest.py`, so CI never installs
  them: one needs a compiler and pins the python version, the other shells out
  to an npm-only binary. Do not "fix" this by installing the real ones.
- **The tests encode the decisions in the rules above, not just behaviour.**
  `test_orders.py` pins the ticket handshake and the caps (rule 3),
  `test_mcp.py` asserts the order tools exist but that `place_order` only ever
  takes a ticket, `test_notify.py` asserts the ntfy topic never reaches
  `status()` (rule 4d/5), and `test_alerts.py` pins both crossing properties
  (rule 4d). If a rule here changes, the test is the other half of the change —
  `test_mcp.py` used to assert that NO order tool existed, and rewriting it was
  part of reversing rule 3, not an afterthought.

## Status

Verified against the live account: positions, guardrails, TDPro signals, chat.

**The order path, market data, streaming and the MCP server have NOT been
exercised against the live account.** They are tested end to end against a stub
broker — `tests/test_orders.py` covers payload shapes, guards, and the ticket
lifecycle, `tests/test_alerts.py` covers the crossing invariants, and
`tests/test_docs.py` catches a module that no longer imports; the MCP server was
driven over stdio through preview → confirm → place → cancel. That proves the wiring, not the broker's acceptance of it.
Webull's own field-level validation, fractional-share and extended-hours rules,
and option-strategy handling are unproven here. First live order should be one
share of something cheap, placed with Webull Desktop open so you can watch it
land.

Market data and streaming are coded against SDK 2.0.16 signatures but not run
against the live entitlement — market data needs a subscription in the regional
Webull app, and the streaming feeds need MQTT/gRPC egress that venus may or may
not have. `quotes.py` degrades to portfolio and TDPro spot when that's missing,
so alerts survive it.

Deployed on venus (`100.113.21.73:8787`) as a boot-enabled user service. The
local Crostini copy binds `127.0.0.1`, which the ChromeOS browser **cannot
reach** — Chrome lives outside the container, and only `penguin` is on the
tailnet, not ChromeOS itself.
