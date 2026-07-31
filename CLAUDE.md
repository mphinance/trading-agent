# CLAUDE.md — webull-sidecar

## What this repo is

A companion deck for **Webull Desktop**: live positions, portfolio guardrails,
TraderDaddy Pro signals, a Claude chat panel, and an **MCP server that lets
Claude Desktop trade the account by voice**. Runs beside the Webull app, not on
top of it.

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
  hard-wired, and it *does* place orders). Build that one for Tradier; this one
  is a read-only Webull companion.
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
- **Voice:** `mcp_server.py` on the official `mcp` SDK (FastMCP), stdio
  transport, for Claude Desktop.
- **Signals:** TraderDaddy Pro over plain JSON-RPC (`POST /api/v1/mcp`). No MCP
  client library — the endpoint takes a bare `tools/call` with no handshake.
  (Unrelated to our own MCP server, despite the name collision.)
- **Chat:** Claude Agent SDK (`claude_agent_sdk`), which shells out to the
  `claude` CLI binary.
- **UI:** one `static/index.html`. Inline SVG/CSS, no deps, no bundler.
- **Deploy:** systemd **user** service bound to a Tailscale IP (see rule 1).

## Critical design rules

### 1. Loopback or Tailscale. Never 0.0.0.0.
This app has **no authentication** and holds live brokerage credentials. Even
though it's read-only, binding it to a LAN lets any device on the wifi read the
account's balances and positions. `deploy/install.sh` *refuses to run* without a
Tailscale IP rather than falling back — the guardrail is in code, not in a
comment. Live on venus at `100.113.21.73:8787`, tailnet-only.

### 2. Secrets live outside the repo, and only in files
`../.env.webull` and `../.env.anthropic` sit in the **parent** directory, so they
cannot be committed by construction. `run.sh` sources them. An `export` in a
shell does **not** reach the server — every Bash call spawns a fresh shell, and
systemd gets its own environment. Audit `git diff --cached` for `sk-ant-` /
`td_live_` before any push.

### 3. The order path is real, and it lives in exactly one file
`orders.py` is the only module that can move money. Everything else reads. If
you are adding a broker write, it goes there or it does not go in.

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

### 4. Inject live state into chat; never make the model fetch it
`WebFetch` upgrades `http://` to `https://`, so it **cannot** read this app's own
loopback server. `chat.py` formats the portfolio + signals into the turn instead.
Faster, no tool round-trip, guaranteed current.

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
wb.py         Webull client — credentials, account/balance/position/order reads,
              caching and rate-limit handling. Owns the shared ApiClient.
md.py         Market data, research, screeners, watchlists (600/min bucket)
orders.py     THE ORDER PATH — guards, preview/confirm tickets, place/replace/cancel
stream.py     MQTT quote push + gRPC trade-event push, bridged onto an SSE bus
risk.py       Portfolio guardrails
td.py         TraderDaddy Pro client (direct JSON-RPC, no MCP library)
chat.py       In-app Claude chat via the Agent SDK. Read-only tools, no order path.
server.py     FastAPI routes
mcp_server.py MCP server for Claude Desktop — a thin HTTP bridge to a running
              sidecar, so there is one broker client and one rate-limit budget
mcp.sh        What Claude Desktop spawns
static/       Single-page UI, no build step
deploy/       systemd unit + Tailscale-bound installer
test_orders.py Order-path tests against a stub broker — no network, no account
docs/API.md   Generated-from-code reference: 31 MCP tools, 37 HTTP endpoints, SSE
              event shapes, the ticket handshake. Regenerate it if you add a
              route or a tool — a stale API doc is worse than none.
```

`mcp_server.py` deliberately does **not** build its own Webull client. Two SDK
clients mean two 2FA token files and two processes racing the same 2 req/2s
account budget. It goes over HTTP to the running sidecar instead, which also
means the guards in `orders.py` apply to voice trades for free.

## Status

Verified against the live account: positions, guardrails, TDPro signals, chat.

**The order path and MCP server have NOT been exercised against the live
account.** They are tested end to end against a stub broker — `test_orders.py`
covers payload shapes, guards, and the ticket lifecycle; the MCP server was
driven over stdio through preview → confirm → place → cancel. That proves the
wiring, not the broker's acceptance of it. Webull's own field-level validation,
fractional-share and extended-hours rules, and option-strategy handling are
unproven here. First live order should be one share of something cheap, placed
with Webull Desktop open so you can watch it land.

Market data, streaming and research are likewise coded against SDK 2.0.16
signatures but not run against the live entitlement — market data needs a
subscription in the regional Webull app, and the streaming feeds need MQTT/gRPC
egress that venus may or may not have.

Deployed on venus (`100.113.21.73:8787`) as a boot-enabled user service. The
local Crostini copy binds `127.0.0.1`, which the ChromeOS browser **cannot
reach** — Chrome lives outside the container, and only `penguin` is on the
tailnet, not ChromeOS itself.
