# sidecar

A companion deck for Webull Desktop. Live positions, P&L, portfolio guardrails,
and an order ticket — in a browser window you park *beside* the Webull app.

![sidecar](docs/screenshot.png)

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
holds live brokerage credentials and can place real orders, with **no
authentication of any kind**. Binding it to `0.0.0.0` lets anyone on the network
trade the account. To reach it from other machines, bind it to a **Tailscale**
address (see Deploy) — device-authenticated, encrypted, invisible to the LAN and
the internet.

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
| `SIDECAR_MAX_NOTIONAL` | `25` | Hard ceiling on any single order placed through the UI. |
| `SIDECAR_CHAT_MODEL` | `claude-sonnet-5` | Chat model. `claude-opus-4-8` for hard questions. |
| `TD_API_KEY` | — | `td_live_…`; lights up the TraderDaddy panels. |

## Layout

```
wb.py            Webull SDK wrapper — credentials, caching, rate-limit handling
risk.py          Portfolio guardrails + pre-trade order checks
td.py            TraderDaddy Pro client (direct JSON-RPC, no MCP library)
chat.py          Claude chat via the Agent SDK; injects live state into each turn
server.py        FastAPI routes
static/          Single-page UI, no build step
deploy/          systemd unit + installer (Tailscale-bound)
```

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

- **Python must be `>=3.8,<3.14`.** The Webull SDK pins this. Ubuntu 22.04 with
  a newer default python3 needs an explicit `python3.10 -m venv .venv`; `run.sh`
  prefers `./.venv/bin/uvicorn` when present.
- **The `claude` CLI must be installed** (`npm i -g @anthropic-ai/claude-code`).
  The *Python* Agent SDK shells out to it and does **not** bundle a binary — only
  the TypeScript SDK does. Without it, chat fails at runtime, not at install.
  `run.sh` adds the nvm bin dir to `PATH` because systemd's PATH is minimal.

## Order safety

Six gates. The pipeline shape is ported from
[TraderDaddy-Desktop](https://github.com/mphinance/TraderDaddy-Desktop)
(`src-tauri/src/commands/orders.rs`); the last three are from reading
[nautilus_trader](https://github.com/nautechsystems/nautilus_trader)'s risk engine.

1. **Arm flag** — session-scoped, **OFF on every boot**, never persisted. A
   restart should never come back armed.
2. **Preview token** — preview mints a single-use UUID bound to the *exact*
   params with a 60s TTL. Place pops it, then checks expiry and that the params
   still match. *This fixed a real bug:* previously a successful preview just
   enabled the Place button, so editing the ticket afterwards let you place an
   order that was never previewed.
3. **Confirm with the account's last 4** — not a fixed word. `PLACE` becomes
   muscle memory and never makes you look at *which* account you're trading.
4. **One `client_order_id`, minted at preview and reused at place.** If a place
   times out ambiguously and the order actually landed, a retry carries the same
   id and Webull rejects the duplicate instead of filling twice. Nautilus won't
   let you bypass duplicate-id checking even with every other risk check off —
   that's the tell.
5. **Oversell check** — a SELL is blocked if `qty > held - already_working_sells`.
   **The notional cap cannot catch this**: selling doesn't consume buying power,
   so a 100-share sell of a 3-share position at $0.20 is $20 of notional and
   passes every dollar-denominated check while opening a 97-share short.
6. **Risk guard** — notional cap + buying power, plus non-blocking cautions
   (averaging down, correlated add, sub-$20 tickets).

Prices are rounded/validated to the SEC Rule 612 tick ($0.01 at/above $1.00,
$0.0001 below) and formatted with `Decimal`, because `str(1e-05)` yields
`'1e-05'` — which is what would have gone on the wire as a price.

Orders are journalled to `orders.jsonl` **before** success is reported. If that
write fails the order is already live, so the response says so rather than
staying silent.

### Deliberately NOT adopted from nautilus

Judged over-engineering at this scale, and worth recording so nobody re-litigates:

- **Fixed-point `Price`/`Quantity`/`Money` types.** The `i128` machinery exists
  for 16-decimal crypto and multi-venue FX. At 2-dp equity prices float64 is
  exact to ~15 significant digits. The lesson was tick-rounding, not the types.
- **An order-state FSM** (~50 transition pairs). Guards against out-of-order
  *event-stream* delivery. We poll; the broker is the source of truth.
- **A submit throttler** (nautilus defaults to 100 orders/sec). One human, one
  button, and the token vault already makes double-submit impossible.
- **A price collar vs last.** Nautilus **doesn't do this either** — its price
  check is precision + positivity only; collars are the venue's job. A limit
  sell at $0.01 executes against the best bid, not at $0.01.
- **Boot reconciliation** (~700 lines). Solves "my engine holds authoritative
  state that drifted while it was down." We hold none — we re-fetch every poll.

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

- **Rate limits are tight and real.** Balance and positions are capped at
  **2 requests / 2 seconds each**. One portfolio poll spends that entire budget
  (one call per endpoint per account × 2 accounts), so `/api/portfolio` and
  `/api/signals` firing together on page load reliably 429s. Fixed with a lock so
  concurrent callers share one fetch, plus retry-with-backoff and a stale
  fallback that serves the last good snapshot rather than a 500. The UI shows
  `stale` in the header when that happens — it never presents old numbers as live.
- **Buying power is shared across accounts**, so totals use `max()`, not `sum()`.
  Summing double-counts the same dollars.
- **`combo_type: "NORMAL"` is mandatory** on equity orders. Webull's own bundled
  sample omits it and gets `417 invalid combo_type`.
- **No market data subscription is required** for any of this — positions,
  balances, and order preview are all trading-API surface. Quotes would need a
  non-display entitlement; see `../docs/README.md`.
