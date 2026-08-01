# Webull OpenAPI reference (vendored)

Third-party docs, copied here so the MQTT/gRPC work has the protocol details
locally instead of re-fetching them mid-build. **Nothing here is ours and
nothing here is authoritative** — if a number in these files disagrees with
what the live API does, the live API wins and the file is stale.

| File | Source | Fetched |
|---|---|---|
| `api-reference.md` | [`webull-inc/webull-openapi-skills`](https://github.com/webull-inc/webull-openapi-skills) `references/api_reference.md` (Apache-2.0) | 2026-08-01 |
| `us-endpoints.md` | same repo, `references/llms_us.md` (Apache-2.0) | 2026-08-01 |
| `mqtt-streaming.md` | [developer.webull.com](https://developer.webull.com/apis/docs/market-data-api/data-streaming-api) | 2026-08-01 |

The skill package itself is deliberately **not** vendored. Its reason to exist
is order placement (`place`, `option-place`, `replace`, `cancel`) guarded only
by a prompt instruction telling the assistant to confirm first, which is the
wrong shape for this repo — see rule 3 in `../../CLAUDE.md`. The reference docs
are the durable part.

## Why these three

- **`mqtt-streaming.md`** is the one that unblocks work. It has the connection
  endpoints, the topic table, the connection limits, and the **protobuf message
  definitions** for `Quote` / `Snapshot` / `Tick`. It is not in the skills repo;
  it only exists in the web docs.
- **`api-reference.md`** has the rate limits and the per-region feature matrix.
- **`us-endpoints.md`** is the US-specific endpoint and order-type summary.

## The parts that bear on this repo

**MQTT is market data only.** Topics are `quote` / `snapshot` / `tick`, all
**protobuf**; only `notice` is JSON and `echo` is a null heartbeat. Balances and
positions stay on the HTTP trading API, so **MQTT does not relieve `wb.py`'s
rate-limit handling** — keep the lock, the backoff, and the stale fallback.
What it can replace is the top of `quotes.py`'s source chain and the polling in
`watcher.py`.

Four constraints that will shape any implementation:

- **5 concurrent connections per App Key** (error `105`). The watcher plus a
  stray experiment gets there fast.
- **The server holds connection state ~1 minute after a disconnect.** A crash
  loop locks itself out rather than reconnecting.
- **Max 3 messages/sec per connection.** Throttled, not a firehose.
- **Subscriptions are NOT restored on reconnect.** They are managed over a
  separate HTTP API, and you must re-subscribe yourself after any drop. This is
  the one most likely to ship as a silent bug: the connection comes back, looks
  healthy, and delivers nothing.

Real-time market data also requires a **paid quote subscription** on the
account; the connection succeeding does not mean data will flow.

**Rate limits (US)** per `api-reference.md`:

```
Auth create/check            10 req/30s
Market data                 600 req/min
Order place/replace/cancel  600 req/min
Order query                   2 req/2s
```

> ⚠️ Open question worth resolving before touching `wb.py`. Our wrapper's
> comment says *"Balance and positions are limited to 2 requests / 2 seconds
> EACH"*, and it builds the whole lock/backoff/stale-fallback machine around
> that. The published table only names **order query** at 2 req/2s and does not
> mention balance or positions. Either account queries share that bucket (in
> which case the comment is right and under-cited) or `wb.py` is being far more
> careful than it needs to be. Measure it against the live account before
> loosening anything — the failure mode of guessing wrong is a 429 storm on the
> one call the UI needs.

**Options:** the feature matrix lists US options trading as supported, and the
platform overview separately says *"Single-Stock Options (excluding Index
Options)"*. If that exclusion holds, **SPX / XSP / VIX are not tradeable over
this API** even though Webull sells them in the app. That is the fact that
decides whether the XSP-based 0DTE playbook survives, and it should be
confirmed against a live chain rather than taken from a doc.
