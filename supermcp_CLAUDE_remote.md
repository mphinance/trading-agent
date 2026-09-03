# CLAUDE.md — supermcp (STALE COPY, kept for reference)

> **This is a copy of another repo's CLAUDE.md, snapshotted before 2026-09-03.**
> Its home is `supermcp` (PRIVATE); this copy is not maintained and is already
> known to be wrong in at least one place (see the Security Alert note below).
> Read the original before trusting anything here, and never edit this file
> expecting it to reach the live system.

Guidance for AI agents working in this repo. Read this first.

## What this is

**supermcp** is a federated **remote MCP server** + a private **holdings/analytics dashboard**
for the Momentum Phund. One Claude custom connector fronts multiple data sources
(Tastytrade, Substack, TDPro) so Michael — and, later, his newsletter
subscribers — can query the book and the writing from Claude anywhere.

**The Momentum Phund IS the Tastytrade account.** IBKR and the old
positions.mphinance.com Phund API were removed 2026-08-25 (the IBKR book was drained
to zero); there is no second venue behind the numbers. Don't reintroduce one.

- **Repo:** `git@github.com:mphinance/supermcp.git` (PRIVATE)
- **Live:** `https://mcp.mphinance.com` — dashboard at `/`, connector at `/mcp`
- **Host:** `vultr` VPS (IP redacted -- this repo is public), behind Apache, systemd unit `supermcp.service`

## The one rule that shapes the design

**Your own account data is yours to share. Live exchange data is not.**
Positions, fills, P&L, trade journal → shareable. Redistributing live exchange quotes/
chains/greeks needs a data license. Broker adapters therefore use delayed/EOD marks or the
user's own already-published feed. See `docs/DATA_POLICY.md`. Don't add a tool that streams
live third-party quotes to subscribers.

## Architecture (how a request flows)

```
Claude (web/mobile/desktop/Code)
  → https://mcp.mphinance.com/mcp   (custom connector; OAuth 2.1 web/mobile OR Bearer SUPERMCP_TOKEN)
  → Apache (SSL, SSE-aware reverse proxy; mirrors ghost.mphinance.com pattern)
  → uvicorn src.app:app on 127.0.0.1:8402  (FastMCP + Starlette custom routes)
      ├─ auth.MultiAuth  (gates /mcp: OAuth 2.1 for web/mobile OR static token)
      ├─ BearerAuth ASGI middleware  (gates /api only; /, /mcp, /health + OAuth routes open here)
      ├─ MCP tools  (@mcp.tool)
      └─ dashboard + JSON API  (@mcp.custom_route)
```

## Layout

| Path | Role |
|------|------|
| `src/app.py` | FastMCP server: tools, HTTP routes, `BearerAuth` middleware, ASGI `app` |
| `src/auth.py` | OAuth 2.1 for web/mobile: `GatedOAuthProvider` |
| `src/config.py` | Loads `.env`; all settings/knobs |
| `src/brokers/tastytrade.py` | Tastytrade adapter (SDK 13.x, delayed marks) |
| `src/brokers/webull.py` | Webull adapter (SDK 2.0.12, live marks & order path) |
| `src/brokers/substack.py` | Substack (cookie `SUBSTACK_SID`): posts & drafts |
| `src/orders.py` | Execution path (`place_live_order`) — currently ungated for Webull |
| `src/tdpro.py` | TraderDaddy Pro integration (`tdpro_*` tools) |
| `src/wallscan.py` | Gamma flush into dealer support wall playbook (`gamma_flush_scan`) |
| `src/holdings.py` | **Default view**: merged holdings across brokers |
| `src/trueup.py` | Deeper reconciliation — **deferred** |
| `src/wheels.py` | Wheel **intent** per ticker (`data/wheels.json`) |
| `web/dashboard.html` | Self-contained bearer-gated dashboard |
| `deploy/` | `supermcp.service` (systemd) + Apache vhost |
| `data/` | Cached state & scans — **gitignored, never commit** |

## MCP tools

**`lookup_ticker(symbol)`** — one-shot dossier: HOLDING (size/PL) · when last WROTE about it
(full-text) + plain-English `summary`.
`search_writings(query)` — full-text Substack search **incl. post bodies**.

**Execution & Orders:**
`place_live_order` (the **only** tool that routes a real fill, currently unguarded for Webull) · `dry_run_order` · `ticket_from_writing`.

**TDPro & Scanners:**
11 `tdpro_*` tools (flow, pulse, signals, gex, screener, quality, pine) · `screen_presets` · `prescan` · `screen_analytics` · `gamma_flush_scan` (via `wallscan.py`) · `scan_history`.

**Account & Ledgers:**
`get_balance` · `get_positions` · **`get_holdings`** (default merged view) · `get_trade_journal` · `get_ledger` · `get_returns` · `get_trade_stats` · `list_writings` · `draft_from_position`.

`set_wheel_state(symbol, state, note)` / `get_wheel_states()` — wheel **intent** only
(`wheeling` · `hold-forever` · `exiting` · `watching`).

HTTP mirrors: `GET /api/holdings|trueup|lookup?symbol=|writings?q=|wheels`,
`POST /api/draft|ticket|wheels`.

> **Free shares are DERIVED, never typed in.** `Tastytrade.net_cash_by_underlying()` sums
> every transaction carrying a symbol (share buys/sales, assignments, called-away proceeds,
> dividends, option premium). Holdings rows carry `net_cash`, `net_basis` (cash still sunk
> per share — negative means house money) and `free`. Note `wheeled_basis` nets only OPTION
> premium, so it can't see called-away equity; use `net_basis` for "what do these actually
> cost me now". Never store a hand-kept quantity or basis — that's how `watched-positions.json`
> drifted into a phantom quantity. `data/wheels.json` holds intent and nothing numeric.

> **Writing search internals:** `Substack.search()` fetches each post's `body_html` once and
> caches the plain text in `data/writings_cache.json` (posts are immutable). Cold search ≈6s
> (warms all 50 posts); warm ≈50ms. Archive `limit` is capped at 50 by Substack (higher → 400).

## Data sources

- **Tastytrade** — direct via SDK, OAuth (`TASTYTRADE_*`). Delayed marks only.
- **Webull** — undocumented SDK (`WEBULL_*`). Used for execution (`place_live_order`).
- **ConnectTrade** — generic connector.
- **TraderDaddy Pro** — internal data lake / API for flow, scans, and GEX.
- **Substack** — undocumented API via `SUBSTACK_SID` cookie (expires; refresh from browser).

## Secrets & config

All in **`.env`** (gitignored — NEVER commit; `data/` too). Copy `.env.example` to start.
Prod source of truth is intended to be VaultGuard (Firebase) later.
Keys: `TASTYTRADE_CLIENT_SECRET/REFRESH_TOKEN/ACCOUNT_ID`, `WEBULL_APP_KEY/APP_SECRET/DID`, `SUBSTACK_SID/PUBLICATION`,
`SUPERMCP_TOKEN` (gateway bearer), `SUPERMCP_PORT` (8402),
`PUBLIC_URL` (OAuth issuer base, default `https://mcp.mphinance.com`),
`OAUTH_PASSWORD` (owner login at `/authorize`; falls back to `SUPERMCP_TOKEN`).

## Run / deploy / verify

```bash
# local
./.venv/bin/python -m uvicorn src.app:app --host 127.0.0.1 --port 8402

# prod (this box)
systemctl restart supermcp.service        # enabled; auto-restarts
journalctl -u supermcp.service -n 30

# smoke test (token from .env)
TOKEN=$(grep '^SUPERMCP_TOKEN=' .env | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" "https://mcp.mphinance.com/api/holdings" | head
```

> After editing `src/`, restart the service and wait ~7s before curling — uvicorn takes a
> few seconds to bind (a fast curl returns 000/503 mid-restart; that's timing, not failure).

## Conventions

- Adapters return a common position shape: `broker, symbol, underlying, type, quantity,
  direction, avg_price, delayed_mark, delayed_pl, cost_basis, market_value` (+ `sector, beta`
  where available). Match it when adding a broker.
- **Security Alert (SUPERSEDED 2026-09-03):** this line said `place_live_order` was routed to Webull *without a secondary gate*. That was fixed on 2026-09-01 -- `UNGATED_ORDER_BROKERS` is now empty and `require_trade_scope()` fails closed. Left here only to record that the stale claim existed; do not cite it as current.
- Adapters must degrade gracefully (empty result on failure), never crash the server.

## Roadmap / phases

1. ✅ Token-gated connector + holdings dashboard (**live**)
2. ✅ Vesper tools (TDPro, Wallscan, Webull) merged in.
3. 🚧 **Consolidation**: Moving all heavy Tier-2+ quant tools and Vesper execution flows (Momentum Phund) out of Webull-sidecar and into `supermcp`.
4. 🚧 Multi-account / bring-your-own-creds for subscribers.
