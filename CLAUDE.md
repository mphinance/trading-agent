# CLAUDE.md — webull-sidecar

## What this repo is

A companion deck for **Webull Desktop**: live positions, portfolio guardrails,
TraderDaddy Pro signals, a single-order ticket, and a Claude chat panel. Runs
beside the Webull app, not on top of it. See [README.md](./README.md) for setup,
deploy, and the full list of API traps.

**This is the org's only real Webull integration.** The one in `trading-dashboard`
(now archived) was a credential form wired to a `setTimeout`. Nothing else here
talks to Webull.

Related repos, and why this isn't merged into them:
- `TraderDaddy-Desktop` — the serious desktop app (Rust/Tauri, Tradier-only,
  hard-wired). Its order-safety pipeline was ported here. Build *that* one for
  Tradier; this one is Webull.
- `traderdaddy-bridge` — a **demo** of a canonical broker schema, not a library.
  Its adapters take injected payload dicts, not credentials; `preview_order`
  echoes the request back; nothing in it can place an order; zero Webull. Do not
  try to host this there.
- `alpha-command-center` — someone else's production algo system. We don't own
  its UI. Not a home for this.

## Stack

- **Backend:** Python + FastAPI + uvicorn. No build step, no frontend framework.
- **Broker:** `webull-openapi-python-sdk` (the official one) — `webull.core` /
  `webull.trade` / `webull.data`.
- **Signals:** TraderDaddy Pro over plain JSON-RPC (`POST /api/v1/mcp`). No MCP
  client library — the endpoint takes a bare `tools/call` with no handshake.
- **Chat:** Claude Agent SDK (`claude_agent_sdk`), which shells out to the
  `claude` CLI binary.
- **UI:** one `static/index.html`. Inline SVG/CSS, no deps, no bundler.
- **Deploy:** systemd **user** service bound to a Tailscale IP (see rule 1).

## Critical design rules

### 1. Loopback or Tailscale. Never 0.0.0.0.
This app has **no authentication** and **can place real orders**. Binding it to a
LAN lets any device on the wifi trade the account. `deploy/install.sh` *refuses
to run* without a Tailscale IP rather than falling back — the guardrail is in
code, not in a comment. Live on venus at `100.113.21.73:8787`, tailnet-only.

### 2. Secrets live outside the repo, and only in files
`../.env.webull` and `../.env.anthropic` sit in the **parent** directory, so they
cannot be committed by construction. `run.sh` sources them. An `export` in a
shell does **not** reach the server — every Bash call spawns a fresh shell, and
systemd gets its own environment. Audit `git diff --cached` for `sk-ant-` /
`td_live_` before any push.

### 3. Six gates before an order goes out
In order: **armed** (session flag, OFF every boot, never persisted) → **confirm**
(last 4 of the account number, not a fixed word) → **preview token** (single-use,
60s TTL, bound to the exact params) → **tick check** → **oversell check** →
**risk guard** (notional cap + buying power).

Three of these exist because a `qty * price > cap` check is blind to them:
- A **duplicate order** — so one `client_order_id` is minted at preview and
  reused at place. Never regenerate it on the place path.
- A **sell** — selling doesn't consume buying power, so 100 shares of a 3-share
  position at $0.20 is $20 of notional and passes every dollar check while
  opening a 97-share short.
- A **malformed price** — off-tick prices are rejected, not silently rounded.

### 4. Inject live state into chat; never make the model fetch it
`WebFetch` upgrades `http://` to `https://`, so it **cannot** read this app's own
loopback server. `chat.py` formats the portfolio + signals into the turn instead.
Faster, no tool round-trip, guaranteed current. Chat is read-only (`Read`,
`Glob`, `Grep`, `WebSearch`, `WebFetch`) — the order path has its own gates and
a chat box must not become a second, unguarded way in.

### 5. Assume this is on video
The user streams this panel. `static/index.html` scrubs anything matching
`sk-ant-…` / `td_live_…` before rendering, and `chat.py` sets
`setting_sources=[]` so the user's `~/.claude` config never leaks into frame.
Prompts are not a guarantee; the scrub is.

### 6. Prefer the subscription credential
`CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) over `ANTHROPIC_API_KEY`.
On the OAuth token there is no per-token cost, so **model choice is free** —
default `claude-sonnet-5`, and don't reach for Haiku to "save money" that isn't
being spent. The SDK still reports `total_cost_usd` on OAuth; it's notional.
Note: the Agent SDK docs say offering claude.ai login *in a product for other
users* needs prior approval — personal use only.

### 7. Don't cargo-cult nautilus
Read `nautilus_trader`'s risk engine, take the three checks in rule 3, and leave
the rest. Explicitly rejected, with reasons, in the README: fixed-point money
types (float64 is exact to ~15 digits at 2-dp equity prices), an order FSM (we
poll; the broker is the source of truth), a submit throttler (one human, one
button), boot reconciliation (we hold no authoritative state), and a price collar
(**nautilus doesn't do this either** — collars are the venue's job).

## Gotchas that will cost you an hour

All verified 2026-07-16 and documented at length in the README:

- **`combo_type: "NORMAL"` is mandatory** on equity orders. Webull's own bundled
  sample omits it → `417 invalid combo_type`.
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
- **`str(1e-05)` is `'1e-05'`** — use `safety.fmt_price` for anything on the wire.
- **Python must be `>=3.8,<3.14`** (Webull SDK pins it). venus defaults to 3.14;
  it runs on a `python3.10` venv.
- **The Python Agent SDK ships no `claude` binary** (only the TS one does).
  `npm i -g @anthropic-ai/claude-code` or chat fails at runtime, not install.

## Layout

```
wb.py       Webull SDK wrapper — credentials, caching, rate-limit handling
risk.py     Portfolio guardrails + pre-trade order checks
safety.py   Arm flag, preview-token vault, tick validation, order journal
td.py       TraderDaddy Pro client (direct JSON-RPC, no MCP library)
chat.py     Claude chat via the Agent SDK; injects live state into each turn
server.py   FastAPI routes
static/     Single-page UI, no build step
deploy/     systemd unit + Tailscale-bound installer
```

## Status

Working and verified against the live account: positions, guardrails, TDPro
signals, chat, order preview. **No real order has been placed through it yet** —
only previews. The order path is tested end-to-end up to the point of submission.

Deployed on venus (`100.113.21.73:8787`) as a boot-enabled user service. The
local Crostini copy binds `127.0.0.1`, which the ChromeOS browser **cannot
reach** — Chrome lives outside the container, and only `penguin` is on the
tailnet, not ChromeOS itself.
