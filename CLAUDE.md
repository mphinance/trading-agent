# CLAUDE.md — webull-sidecar

## What this repo is

A companion deck for **Webull Desktop**: live positions, portfolio guardrails,
TraderDaddy Pro signals, and a Claude chat panel. Runs beside the Webull app,
not on top of it. **Read-only — sidecar cannot place, modify, or cancel an
order.** All trading still happens in Webull Desktop; this app only reads the
account. See [README.md](./README.md) for setup, deploy, and the full list of
API traps.

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
- **Broker:** `webull-openapi-python-sdk` (the official one) — `webull.core` /
  `webull.trade` / `webull.data`, read-only calls only (accounts, balances,
  positions).
- **Signals:** TraderDaddy Pro over plain JSON-RPC (`POST /api/v1/mcp`). No MCP
  client library — the endpoint takes a bare `tools/call` with no handshake.
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
`../.env.webull`, `../.env.anthropic` and `../.env.telegram` sit in the **parent**
directory, so they cannot be committed by construction. `run.sh` sources them. An `export` in a
shell does **not** reach the server — every Bash call spawns a fresh shell, and
systemd gets its own environment. Audit `git diff --cached` for `sk-ant-` /
`td_live_` before any push.

### 3. There is no order path. Keep it that way.
sidecar reads accounts, balances, and positions — nothing in `wb.py` calls
`preview_order`, `place_order`, or `cancel_order`. Chat is read-only too
(`Read`, `Glob`, `Grep`, `WebSearch`, `WebFetch`) — it can discuss the numbers
it's given, not act on them. Don't reintroduce an order ticket, an arm flag, or
any write path to the broker without treating it as a new feature with its own
threat model, not a small addition to this one.

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

- **Rate limits are tight**: balance and positions are **2 req / 2s each**. One
  poll spends the whole budget across two accounts. `wb.py` uses a lock so
  concurrent callers share one fetch, plus backoff and a stale fallback.
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
- **Python must be `>=3.8,<3.14`** (Webull SDK pins it). venus defaults to 3.14;
  it runs on a `python3.10` venv.
- **The Python Agent SDK ships no `claude` binary** (only the TS one does).
  `npm i -g @anthropic-ai/claude-code` or chat fails at runtime, not install.

## Layout

```
wb.py          Webull SDK wrapper — credentials, caching, rate-limit handling (read-only)
risk.py        Portfolio guardrails
td.py          TraderDaddy Pro client (direct JSON-RPC) + dealer-gamma levels
chat.py        Claude chat via the Agent SDK; injects live state into each turn
alerts.py      Alert store + crossing logic (a level can BE the dealer structure)
quotes.py      Last price: Webull data -> portfolio -> TDPro spot, tagged by source
watcher.py     Background thread that evaluates alerts and delivers them
notify.py      Alert delivery: ntfy (no signup) and/or Telegram
mcp_server.py  Claude Desktop MCP server (stdio, thin client over the HTTP API)
server.py      FastAPI routes
static/        Single-page UI, no build step
deploy/        systemd unit + Tailscale-bound installer
```

## Status

Working and verified against the live account: positions, guardrails, TDPro
signals, chat. Read-only — there is no order path to test.

Deployed on venus (`100.113.21.73:8787`) as a boot-enabled user service. The
local Crostini copy binds `127.0.0.1`, which the ChromeOS browser **cannot
reach** — Chrome lives outside the container, and only `penguin` is on the
tailnet, not ChromeOS itself.
