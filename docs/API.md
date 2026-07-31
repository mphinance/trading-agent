# sidecar API reference

Three surfaces, one broker client behind all of them:

| Surface | Who uses it | Where |
| --- | --- | --- |
| **MCP tools** (36) | Claude Desktop, by voice | `mcp_server.py` over stdio |
| **HTTP API** (42 endpoints across 39 paths) | the deck's UI, scripts, curl | `server.py` on `:8787` |
| **SSE stream** | anything wanting push instead of poll | `GET /api/stream` |

The MCP server is a thin HTTP client of the second one, so anything here is
reachable by voice and vice versa. Nothing bypasses the guards in `orders.py`.

**Rate-limit budgets are not shared.** US region: account reads (portfolio,
orders) are **2 req / 2s**; market data is **600 req/min**; order
place/replace/cancel is **600 req/min**. Poll quotes freely; poll positions
gently.

---

## MCP tools

What Claude Desktop sees. Descriptions are written for a model to act on, so
they carry the constraints inline — read `mcp_server.py` for the full text.

### Account

| Tool | Required | Returns |
| --- | --- | --- |
| `get_portfolio` | — | Positions, balances, totals, and the risk guardrails. Start here for "what do I own". |
| `get_activities` | — | Fills, dividends, transfers, fees. Optional `account_id`. |
| `get_market_calendar` | — | Trading days and session times. `market_code` defaults to `US`. |
| `get_health` | — | Webull connectivity, account count, credential status, trading on/off, stream state. |

### Market data

| Tool | Required | Notes |
| --- | --- | --- |
| `get_quote` | `symbols` | Comma-separated. Includes extended-hours prints. |
| `get_bars` | `symbol` | `timespan` M1/M5/M15/M30/M60/D/W/M, `count`, `sessions` RTH/PRE/ATH. |
| `get_order_book` | `symbol` | Level 2 depth. `levels` defaults to 5. |
| `get_time_and_sales` | `symbol` | Tick-by-tick prints. |
| `get_order_flow` | `symbols` | Footprint: buy vs sell volume per price level. |
| `get_auction_imbalance` | `symbol` | NASDAQ NOII. Empty outside the call auctions — that's normal, not an error. |

### Options

| Tool | Required | Notes |
| --- | --- | --- |
| `get_option_chain` | `underlying` | `expire_date` (YYYY-MM-DD, exact), `option_type` CALL/PUT, `strike_gte`/`strike_lte`, `page_size`. Use this to find option symbols before quoting or trading. |
| `get_option_quote` | `symbols` | By option symbol, comma-separated. |

### Research & screening

| Tool | Required | Notes |
| --- | --- | --- |
| `get_research` | `kind`, `symbol` | `kind`: `profile`, `rating`, `target`, `flow`, `earnings`, `dividend`, `filings`, `eps`, `peers`, `financials`. For `financials` set `statement` to `indicators`/`income`/`cashflow`/`balance`/`alert`. |
| `run_screener` | `kind` | `gainers`, `losers`, `active`, `sectors`, `dividend`, `52whl`. |
| `get_signals` | — | TraderDaddy Pro: market health, put/call ratios, conviction. `configured: false` without `TD_API_KEY`. |
| `get_gamma` | `symbol` | Dealer-gamma structure: spot, regime, flip, max-gamma pin, key levels, heaviest strikes. |

**`get_gamma` is a positioning map, not a forecast.** Heavy gamma marks where
price often *reacts* on arrival; it never means price will travel there. The flip
is a regime boundary — above it dealer hedging dampens moves, below it amplifies
them. If `flip_split` is true the two upstream models disagree about which side
price is on, so the regime call is genuinely uncertain and should be reported
that way rather than resolved silently. A cold non-index name is computed
upstream on first call (~2-4s); `td.levels()` caches for 5 minutes.

### Alerts

| Tool | Required | Notes |
| --- | --- | --- |
| `list_alerts` | — | Every alert with its resolved level and state, plus watcher and delivery status. |
| `create_alert` | `symbol`, `level`, `direction` | `level` is a number **or** a live dealer level: `flip`, `pin`, `wall_above`, `wall_below`. `direction` is the side price must end on. `note`, `repeat` optional. |
| `delete_alert` | `alert_id` | Id comes from `list_alerts`. |
| `test_alert_delivery` | — | Sends a test notification to prove the delivery path works. |

A dynamic level is re-resolved from TDPro on every check, which is the point —
native alert systems freeze a number, and dealer structure moves daily. Alerts
fire on a **crossing**, not a comparison: one armed on the wrong side of a level
starts `pending` and arms only once price returns, so it reports a genuine break
instead of firing instantly. A moving level can never fire an alert on its own.

**Alerts are not evaluated by MCP.** A stdio server only runs while Claude
Desktop is talking to it; the watching is sidecar's own background thread. These
tools arm and inspect only.

### Watchlists

`get_watchlists`, `get_watchlist_items(watchlist_id)`, `create_watchlist(name)`,
`add_to_watchlist(watchlist_id, instruments)`,
`remove_from_watchlist(watchlist_id, instruments)`.

These write, but only to a list of tickers — no account impact. Changes sync to
the Webull app.

### Orders — reads

| Tool | Notes |
| --- | --- |
| `get_open_orders` | Working orders across all accounts — **including ones placed in Webull Desktop**. The account is the source of truth, not this app. |
| `get_order_history` | Filled, cancelled, rejected. `page_size` defaults to 50. |
| `get_trading_config` | The caps currently in force. Worth checking before explaining a rejection. |

### Orders — writes

| Tool | Required | Notes |
| --- | --- | --- |
| `preview_order` | `symbol`, `side`, `quantity` | Stages a ticket. Does **not** send. |
| `preview_option_order` | `legs`, `quantity` | Single or multi-leg. Stages a ticket. |
| `place_order` | `ticket_id` | **Sends.** Real money. |
| `discard_ticket` | `ticket_id` | Drop a staged ticket. |
| `replace_order` | `account_id`, `client_order_id` | Amend quantity or price. Guards re-run. |
| `cancel_order` | `account_id`, `client_order_id` | Never capped — reducing risk is always allowed. |
| `cancel_all_orders` | — | Clears resting orders. Does *not* close positions. |
| `place_order_now` | `symbol`, `side`, `quantity` | One-shot. Only works with `SIDECAR_ORDER_CONFIRM=0`; otherwise returns a rejection telling you to preview first. |

`preview_order` parameters: `order_type` (MARKET, LIMIT, STOP_LOSS,
STOP_LOSS_LIMIT, TRAILING_STOP_LOSS), `limit_price`, `stop_price`,
`time_in_force` (DAY, GTC, IOC), `trading_session` (CORE, ALL, NIGHT),
`take_profit` / `stop_loss` (either one turns it into a bracket), `algo_type`
(TWAP, VWAP, POV — US only), `account_id`.

`preview_option_order` legs: each needs `symbol` (the *underlying*, e.g. `TSLA`),
`strike_price`, `option_expire_date`, `option_type` (CALL/PUT), `side`,
`quantity`. `option_strategy`: SINGLE, VERTICAL, STRADDLE, STRANGLE, CALENDAR,
DIAGONAL, BUTTERFLY, CONDOR, IRON_CONDOR.

---

## The ticket handshake

Two steps, and the reason is the same one that makes it good for voice: no
single call can both construct an order and fire it, and what gets confirmed out
loud is byte-for-byte what reaches the broker.

```
preview_order(...)  ──► { ticket_id, summary, preview, expires_in, orders }
                          │
                     read `summary` back to the user, wait for a yes
                          │
place_order(ticket_id) ──► { ok: true, orders, response }
```

The ticket holds a SHA-256 of the exact payload. `place` re-checks that digest,
marks the ticket used, and refuses a second call. Tickets expire after **120
seconds** — preview again rather than trying to reuse one.

A broker-side rejection un-marks the ticket so the same confirmed order can be
retried without re-confirming.

Response shape from `preview`:

```json
{
  "ticket_id": "0f3c…",
  "kind": "equity",
  "account_id": "…",
  "summary": "BUY 2 ONDS @ 8.40 DAY CORE (TP 11.5 / SL 10)",
  "origin": "ui",
  "orders": [ { "client_order_id": "…", "combo_type": "NORMAL", … } ],
  "preview": { "estimated_cost": "16.80", "buying_power_effect": "-16.80" },
  "expires_in": 119,
  "used": false
}
```

`orders` is the literal payload that will be sent — inspect it if you want to
know exactly what Webull receives.

### Guard rejections

Guards run **server-side on every path**, so MCP, HTTP and anything else get the
same answers. A rejection comes back as an HTTP 400 with a plain message, which
the MCP layer passes through verbatim so it can be read aloud:

```
order notional ~$75,600.00 exceeds SIDECAR_MAX_NOTIONAL ($2,500.00).
Raise the cap deliberately if you mean it.
```

Rejections are answers, not transport failures — relay them rather than retrying
blindly. `replace` re-runs the guards (amending an order can raise exposure);
`cancel` never does. Market orders are priced from the live quote before the cap
is applied, so leaving off a limit price doesn't dodge it.

---

## HTTP API

### Account

| Method | Path | Query |
| --- | --- | --- |
| GET | `/api/portfolio` | `live` (default true — overlays live quotes onto positions) |
| GET | `/api/activities` | `account_id` |
| GET | `/api/calendar` | `market_code` |
| GET | `/api/health` | — |

### Market data

| Method | Path | Query |
| --- | --- | --- |
| GET | `/api/quote` | `symbols`, `category` |
| GET | `/api/bars` | `symbol`, `category`, `timespan`, `count`, `sessions` |
| GET | `/api/depth` | `symbol`, `category`, `levels` |
| GET | `/api/tick` | `symbol`, `category`, `count` |
| GET | `/api/footprint` | `symbols`, `category`, `timespan`, `count` |
| GET | `/api/noii` | `symbol`, `action_type` |
| GET | `/api/instrument` | `symbols`, `category` |
| GET | `/api/options/chain` | `underlying`, `expire_date`, `option_type`, `strike_gte`, `strike_lte`, `page_size` |
| GET | `/api/options/quote` | `symbols` |

`category`: `US_STOCK` (default), `US_ETF`, `US_OPTION`, `US_CRYPTO`,
`US_FUTURES`, `US_EVENT`.

### Structure & alerts

| Method | Path | Query / body |
| --- | --- | --- |
| GET | `/api/gex/{symbol}` | — (symbol must match `^[A-Za-z]{1,6}$`) |
| GET | `/api/alerts` | — |
| POST | `/api/alerts` | `{symbol, level, direction, note?, repeat?}` |
| DELETE | `/api/alerts/{alert_id}` | — |
| POST | `/api/alerts/test` | — |

### Research, screening, watchlists

| Method | Path | Query / body |
| --- | --- | --- |
| GET | `/api/research/{kind}` | `symbol`, `statement`, `count` |
| GET | `/api/screener/{kind}` | `category`, `page_size`, `rank_type`, `sort_by` |
| GET | `/api/watchlists` | — |
| GET | `/api/watchlists/{id}` | — |
| POST | `/api/watchlists` | `{"name": "..."}` |
| DELETE | `/api/watchlists/{id}` | — |
| POST | `/api/watchlists/{id}/add` | `{"instruments": [...]}` |
| POST | `/api/watchlists/{id}/remove` | `{"instruments": [...]}` |

### Orders

| Method | Path | Body |
| --- | --- | --- |
| GET | `/api/orders/config` | — |
| GET | `/api/orders/open` | — |
| GET | `/api/orders/history` | `page_size` |
| GET | `/api/orders/tickets` | — (staged, unused tickets) |
| POST | `/api/orders/preview` | order spec |
| POST | `/api/orders/place` | `{"ticket_id": "..."}` |
| POST | `/api/orders/place_direct` | order spec — 400s unless `SIDECAR_ORDER_CONFIRM=0` |
| DELETE | `/api/orders/tickets/{id}` | — |
| POST | `/api/orders/replace` | `{account_id, client_order_id, quantity?, limit_price?, stop_price?, kind?}` |
| POST | `/api/orders/cancel` | `{account_id, client_order_id, kind?}` |
| POST | `/api/orders/cancel_all` | — |

Order spec (equity), as accepted by `/api/orders/preview` and
`/api/orders/place_direct`:

```json
{
  "symbol": "ONDS",
  "side": "BUY",
  "quantity": 2,
  "order_type": "LIMIT",
  "limit_price": 8.40,
  "time_in_force": "DAY",
  "trading_session": "CORE",
  "bracket": { "take_profit": 11.5, "stop_loss": 10.0 },
  "kind": "equity"
}
```

A `bracket` expands server-side into a Webull combo: `MASTER` +
`STOP_PROFIT` + `STOP_LOSS`, with the children taking the opposite side and the
same quantity as the entry.

### Signals & chat

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/signals` | TraderDaddy snapshot |
| GET | `/api/chat/status` | Credential kind and model |
| POST | `/api/chat` | SSE stream of a chat turn. Body takes an optional `symbol` — the focused gamma name, whose levels ride along with the turn so voice questions need no ticker. |

The chat panel has **no order tools** — see rule 3 in [CLAUDE.md](../CLAUDE.md).

---

## SSE stream

`GET /api/stream` — a single event stream carrying both push feeds. On connect
it replays the last known value per topic, so a freshly opened client renders
immediately instead of waiting for every symbol to tick. A `: keepalive` comment
goes out every 20s.

```js
const es = new EventSource("/api/stream");
es.onmessage = e => {
  const ev = JSON.parse(e.data);
  if (ev.type === "quote") { /* ev.symbol, ev.last, ev.bid, ev.ask */ }
  if (ev.type === "event") { /* ev.kind: order | position | option */ }
  if (ev.type === "stream") { /* ev.feed, ev.state: connected | disconnected */ }
};
```

**Quote** (MQTT, `DataStreamingClient`) — subscribed to whatever the account
currently holds, retargeted as positions change:

```json
{"type":"quote","key":"quote:ONDS","symbol":"ONDS","last":8.61,
 "bid":8.60,"ask":8.62,"volume":1000,"change":0.1,"change_pct":0.01,"at":1.75e9}
```

**Trade event** (gRPC, `TradeEventsClient`) — the one worth wiring up. An order
you place *in Webull Desktop* pushes here in about a second, so the deck stops
being a polled approximation of what the desktop already knows:

```json
{"type":"event","kind":"order","subscribe_type":"...","payload":{...},"at":1.75e9}
```

`kind` is `order`, `position`, or `option`. Payloads are scrubbed of
credential-shaped keys before they leave the server.

`GET /api/stream/status` reports per-feed connection state, last message time,
and subscriber count.

Both feeds are optional and degrade rather than break: if MQTT can't connect,
the REST snapshot path in `md.py` keeps prices correct, just less immediate.
Streaming needs MQTT and gRPC egress, which a locked-down host may not have.

---

## Notes

- **Reads are cached** to fit the rate limits: quotes 1s, bars 30s, screeners
  60s, research 15min, portfolio 8s, orders 4s. A fill invalidates the portfolio
  and open-order caches immediately — serving a cached snapshot after an order
  lands would misreport holdings.
- **Market data needs a subscription** in the regional Webull app. Positions,
  balances and the order path do not.
- **`instrument_type` vs `category`.** Position rows use `EQUITY`/`OPTION`/
  `CRYPTO`; market-data calls want `US_STOCK`/`US_OPTION`/`US_CRYPTO`.
  `md.category_for()` maps between them.
