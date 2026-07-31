# sidecar

A companion deck for Webull Desktop. Live positions, P&L, portfolio guardrails,
TraderDaddy Pro signals, and a Claude chat panel — in a browser window you park
*beside* the Webull app.

It also ships an **MCP server**, so Claude Desktop can read the account and
place orders on your behalf. The rig it's built for is three windows:

| Window | Does |
| --- | --- |
| **Webull Desktop** | Charts, manual trading, the thing you already trust |
| **sidecar** | The deck — positions, guardrails, signals, live prices |
| **Claude Desktop** | Voice. Connected to sidecar's MCP server. |

You say *"what am I holding?"* or *"buy two ONDS at eight forty."* Claude calls
sidecar, sidecar calls Webull.

**sidecar can place orders.** Ordering is two-step by default — Claude previews,
reads the order back, and only sends after you say yes — and every order runs a
notional cap and a quantity cap first. See [Trading](#trading).

📖 **[docs/API.md](docs/API.md)** — every MCP tool, every HTTP route, the ticket
handshake, and the SSE stream format.

![sidecar](docs/screenshot.png)

*Portfolio, guardrails, options flow, and conviction figures above are
illustrative sample data, not a real account.*

## Why "sidecar" and not an overlay

A floating always-on-top overlay **cannot work on ChromeOS Crostini**. Linux apps
run in an LXC container; Sommelier proxies each window out to ChromeOS's own
compositor, where every Linux window becomes an ordinary ChromeOS window.
ChromeOS owns stacking and won't honour a container app's always-on-top request.
Xwayland here is `-rootless`, so there's no root window to draw on and the usual
X11 overlay tricks (override-redirect, XShape click-through) have nothing to bite
on. Webull Desktop is also Qt5 + QtWebEngine — no Electron `.asar`, no JS to
inject, no devtools port — so modifying its UI would mean binary patching, which
is fragile across upgrades and against Webull's ToS.

So sidecar sits *next to* Webull instead, reading the same account over the
OpenAPI. Nothing it shows depends on the desktop app running at all.

## Run

```bash
./run.sh                 # http://127.0.0.1:8787
```

**Loopback only by default, and that default is load-bearing.** This process
holds live brokerage credentials, with **no authentication of any kind**, and it
can now trade. Binding it to `0.0.0.0` hands anyone on the network the ability
to read your balances *and place orders in your account*. To reach it from other
machines, bind it to a **Tailscale** address (see Deploy) — device-authenticated,
encrypted, invisible to the LAN and the internet. `deploy/install.sh` refuses to
run without one.

If you want the deck without the order path, start it with `SIDECAR_TRADING=0`.

### Voice (Claude Desktop over MCP)

Start sidecar first — the MCP server is a thin bridge to it, not a second broker
client. Then add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "webull-sidecar": {
      "command": "/home/YOU/webull-sidecar/mcp.sh"
    }
  }
}
```

Restart Claude Desktop and you get 31 tools: portfolio, quotes, bars, depth,
time & sales, option chains, research, screeners, watchlists, orders — all
listed in [docs/API.md](docs/API.md). Point it at a remote sidecar with
`SIDECAR_URL=http://100.113.21.73:8787`.

Then talk to it:

> **You:** what am I holding?
> **You:** pull up the ONDS chain for next Friday
> **You:** buy two ONDS at eight forty with a stop at seven ninety

### Credentials

Secrets live **outside this repo**, in the parent directory, so they cannot be
committed by construction:

| File | Contents |
| --- | --- |
| `../.env.webull` | `WEBULL_KEY`, `WEBULL_SECRET` (required); `TD_API_KEY` (optional) |
| `../.env.anthropic` | `CLAUDE_CODE_OAUTH_TOKEN` (preferred) **or** `ANTHROPIC_API_KEY` |

`run.sh` sources both. An `export` in your shell does **not** reach the server —
it must be in the file.

### Optional env

| Variable | Default | Purpose |
| --- | --- | --- |
| `SIDECAR_HOST` | `127.0.0.1` | Bind address. Use a Tailscale IP to share; never `0.0.0.0`. |
| `SIDECAR_PORT` | `8787` | Listen port. |
| `SIDECAR_CHAT_MODEL` | `claude-sonnet-5` | Chat model. `claude-opus-4-8` for hard questions. |
| `TD_API_KEY` | — | `td_live_…`; lights up the TraderDaddy panels. |
| `SIDECAR_URL` | `http://127.0.0.1:8787` | Where `mcp.sh` finds a running sidecar. |

### Trading env

| Variable | Default | Purpose |
| --- | --- | --- |
| `SIDECAR_TRADING` | `1` | `0` disables the order path entirely — the kill switch. |
| `SIDECAR_ORDER_CONFIRM` | `1` | `0` allows one-shot placement, skipping the ticket handshake. |
| `SIDECAR_MAX_NOTIONAL` | `2500` | Max $ per order. Rejected before Webull sees it. |
| `SIDECAR_MAX_QUANTITY` | `10000` | Max shares/contracts per order. |
| `SIDECAR_MAX_BP_FRACTION` | `1.0` | Cap an order at this fraction of buying power. |
| `SIDECAR_SYMBOL_ALLOWLIST` | — | Comma-separated. Empty means any symbol. |

## Trading

The order path lives in `orders.py` and nowhere else.

**Two steps, by design.** `preview_order` validates, runs the caps, asks Webull
to price it, and stages a *ticket* holding a SHA-256 of the exact payload.
`place_order(ticket_id)` sends it. No single call can both build and fire an
order, and what you confirm out loud is byte-for-byte what reaches the broker.
Tickets are single-use and expire after 120 seconds.

Spoken, that's:

> **You:** buy two ONDS at eight forty
> **Claude:** *(preview_order)* BUY 2 ONDS @ 8.40 DAY CORE — estimated cost
> $16.80, buying power effect −$16.80. Send it?
> **You:** yes
> **Claude:** *(place_order)* Sent.

**What's supported:** market, limit, stop, stop-limit, trailing stop; brackets
(take-profit and stop-loss attached to the entry, sent as a Webull combo);
TWAP/VWAP/POV algo orders (US only); single- and multi-leg options — verticals,
straddles, condors; replace and cancel; cancel-all.

**What the guards do.** Caps run server-side on every path, so they apply to
voice, HTTP, and anything else equally. `replace` re-runs them (amending an
order can raise exposure); `cancel` never does (reducing risk is always
allowed). Market orders are priced from the live quote before the cap is
checked, so you can't dodge it by leaving off a limit price.

**What is *not* wired to the order path:** the in-app chat panel. It holds
`WebFetch`/`WebSearch`, so it reads text written by strangers, and a component
with attacker-controllable input should not hold your account. It can propose a
trade; it cannot send one. Claude Desktop over MCP is a different case — it acts
on your voice, not on a page it fetched.

> **Not exercised against a live account yet.** The order path is tested end to
> end against a stub broker (`test_orders.py`, plus an MCP stdio run through
> preview → confirm → place → cancel). That proves the wiring, not Webull's
> acceptance of it. Make the first real order one share of something cheap, with
> Webull Desktop open to watch it land.

## Layout

```
wb.py            Webull client — credentials, account/order reads, caching, rate limits
md.py            Market data, research, screeners, watchlists (600/min bucket)
orders.py        The order path — guards, preview/confirm tickets, place/replace/cancel
stream.py        MQTT quote push + gRPC trade events, bridged to SSE
risk.py          Portfolio guardrails
td.py            TraderDaddy Pro client (direct JSON-RPC, no MCP library)
chat.py          In-app Claude chat via the Agent SDK. Read-only, no order path.
server.py        FastAPI routes
mcp_server.py    MCP server for Claude Desktop — HTTP bridge to a running sidecar
mcp.sh           What Claude Desktop spawns
test_orders.py   Order-path tests against a stub broker (no network, no account)
static/          Single-page UI, no build step
deploy/          systemd unit + installer (Tailscale-bound)
docs/API.md      Full MCP tool + HTTP route + SSE stream reference
```

`mcp_server.py` deliberately has no Webull client of its own. Two SDK clients
means two 2FA token files and two processes racing the same 2 req/2s account
budget; going over HTTP to the running sidecar keeps one client, one cache, and
makes the `orders.py` guards apply to voice trades for free.

## Deploy (Tailscale-bound, starts at boot)

```bash
rsync -az --exclude __pycache__ --exclude .venv sidecar/ host:~/webull/sidecar/
rsync -az .env.webull .env.anthropic host:~/webull/     # chmod 600 on arrival
ssh host '~/webull/sidecar/deploy/install.sh'
```

`install.sh` reads the host's Tailscale IP, writes `deploy/sidecar.env`, installs
a **user** systemd unit, and enables it. It **refuses to run without a Tailscale
IP** rather than falling back to `0.0.0.0` — the guardrail is in code, not in a
comment.

The unit is a user service (needs `loginctl enable-linger $USER`, which
`install.sh` sets): starts at boot with no root and no login session, restarts on
failure, and orders after `tailscaled` so the bind doesn't race at startup.

### Host prerequisites — both bit on the first deploy

- **Python 3.8–3.14.** This used to top out below 3.14; SDK 2.0.16 declares
  `python_requires='>=3.8,<3.15'` and pins cryptography/grpcio explicitly for
  3.14, so a modern default python3 is fine now. `run.sh` still prefers
  `./.venv/bin/uvicorn` when a venv is present, and an existing `python3.10`
  venv needs no rebuild.
- **The `claude` CLI must be installed** (`npm i -g @anthropic-ai/claude-code`).
  The *Python* Agent SDK shells out to it and does **not** bundle a binary — only
  the TypeScript SDK does. Without it, chat fails at runtime, not at install.
  `run.sh` adds the nvm bin dir to `PATH` because systemd's PATH is minimal.

## Guardrails

| Check | Fires when |
| --- | --- |
| Concentration | Largest position > 25% (caution) / 35% (alert) of NLV |
| Correlated exposure | Two holdings track the same underlying — see `RELATED` in `risk.py` |
| Drawdown | Book > 10% (caution) / 25% (alert) underwater on cost |
| Dry powder | Buying power < 10% of NLV |
| Breadth | Every position red |

`RELATED` currently maps `ONDL → ONDS` (leveraged ETF and its underlying). Extend
that dict as the book changes — the check is only as good as the map.

## Claude / Agent SDK gotchas (verified 2026-07-16)

- **`claude setup-token` produces a token starting `sk-ant-oat…`, which is NOT an
  API key.** It shares the `sk-ant-` prefix and the ~108-char length of a real
  API key, so every structural check passes — but putting it in
  `ANTHROPIC_API_KEY` yields `401 invalid x-api-key`, because OAuth tokens don't
  go in the `x-api-key` header. It belongs in `CLAUDE_CODE_OAUTH_TOKEN`. Cost us
  an hour; the credential was fine the whole time.
- **The Agent SDK reports auth failures uselessly.** It raised
  `Claude Code returned an error result: success`. The real signal was in the
  message stream: an `AssistantMessage` with `error='authentication_failed'`.
  When debugging auth, test the credential **directly** against the API — the
  clean `401` tells you more than the SDK does.
- **`total_cost_usd` is reported even on the OAuth token**, where the
  subscription covers it. Don't render it as money owed; the UI labels it
  "subscription — no charge (would bill ~$X on API)".
- **WebFetch upgrades `http://` to `https://`**, so it cannot read a plain-HTTP
  loopback server. Don't have the model fetch state this app already holds —
  `chat.py` injects portfolio/signals into the turn instead. Faster, no tool
  round-trip, guaranteed current.
- **`setting_sources=[]`** keeps the user's `~/.claude` config (CLAUDE.md,
  skills) out of a panel that gets streamed on video.

## TraderDaddy API gotchas (verified 2026-07-16)

- **`get_conviction` takes `symbol`, not `ticker`.** An unknown key is silently
  ignored, and omitting `symbol` is exactly how you request the *market-wide*
  gauge — so passing `{"ticker": "MULL"}` returns the market payload, and five
  "per-ticker" calls all come back byte-identical with the same score. If every
  ticker scores the same, this is why.
- **Conviction is 0–100 with 50 = neutral** — a diverging scale, not a magnitude.
  Encode it from the centre. A score of exactly 50 with a single
  `watchlistMomentum` pillar is the no-signal default, not a real reading; the UI
  marks those "no data" rather than drawing a confident neutral bar.
- **`compositeScore` is an object**, `{value, max, label}` — not a number. And
  **higher is worse** (it counts tripped signals), so the colour scale runs the
  opposite way to a 0–100 score.
- **Responses are UTF-8 but don't always declare a charset**, so `requests` falls
  back to ISO-8859-1 and em-dashes arrive as `â€"`. `td.py` pins `r.encoding` and
  decodes `r.content` explicitly.
- The endpoint takes a bare `tools/call` with **no initialize handshake**, so one
  POST per call — no MCP client library needed.

## Visualisation notes

Palette and forms follow the dataviz method rather than taste:

- The six categorical series hues are the reference palette stepped for dark,
  **validated against this navy surface** (`#0a0f19`) — lightness band, chroma
  floor, CVD separation (worst adjacent ΔE 8.4 protan), normal-vision floor
  (19.3), and 3:1 contrast all pass. Hues are assigned per entity in fixed order
  and never cycled or re-sorted by rank.
- **Allocation** is part-to-whole → horizontal stacked bar with 2px surface gaps
  and a legend (identity is never colour-alone).
- **Guardrails** are ratios against a limit → meters with a limit tick. The tick
  is what makes the number mean something.
- **Put/call and conviction** have neutral midpoints (1.0 and 50) → diverging
  from centre. IWM's put/call regularly runs off scale; it's clamped at 2.0 with
  an overflow marker so one outlier can't flatten the other two.
- Status colours ship with an icon and label, never colour alone.

## Notes from building it

- **The tight rate limit is one bucket, not the whole API.** This cost an hour
  because the 2 req/2s figure got generalised. US region, from Webull's own
  reference:

  | Bucket | Limit |
  | --- | --- |
  | Order query — *and this is where balance and positions live* | **2 req / 2s** |
  | Market data | 600 req / min |
  | Order place / replace / cancel | 600 req / min |
  | Auth create/check | 10 req / 30s |

  So the account endpoints are ~100× scarcer than quotes. `wb.py` guards the
  scarce bucket with a lock (concurrent callers share one fetch),
  retry-with-backoff, and a stale fallback that serves the last good snapshot
  rather than a 500 — the UI shows `stale` in the header rather than presenting
  old numbers as live. `md.py` is a separate cache on the generous bucket, which
  is why live quotes can refresh every second without starving the portfolio
  poll. Keep them separate.
- **Buying power is shared across accounts**, so totals use `max()`, not `sum()`.
  Summing double-counts the same dollars.
- **Positions and balances need no market data subscription** — they're
  trading-API surface. *Quotes are different*: `md.py`, the streaming feeds, and
  anything in the MCP server that returns a price need a market data
  subscription in the regional Webull app, and a non-display entitlement for
  some uses. See `../docs/README.md`. The order path itself needs none of it.
