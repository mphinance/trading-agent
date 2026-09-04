# The funnel plan

**Revision 4, 2026-09-03** (updated same day: §3 and §7 fixed and deployed; §6 rewritten once `momentum-mcp` was found). Rewritten, not patched. Revisions 1-3 each got the
shape wrong in a different way; the change log is §10. This one starts from what
was actually verified in the code and against the live boxes.

---

## 1. The strategy, in one page

**Research is free. Live positioning is paid.**

| | what | cost to serve |
|---|---|---|
| **Free** | ~37 quant tools: screeners (CANSLIM / VCP / PEAD), 24 technical indicators, backtesting + walk-forward, EDGAR filings and XBRL, breadth, macro regime, market-top detection, position sizing, pair trade, Monte Carlo | **nothing** |
| **Paid** | dealer gamma, apex levels, options flow, dark pool, directional flow, conviction | your TMpro API capacity |

The economics are what make this work, and they are the thing earlier revisions
missed. An MCP server runs **on the user's own machine**. Those 37 tools call
yfinance, TradingView and EDGAR directly from there, and compute the rest
locally. They consume **zero TMpro API calls, zero rate limit, zero infra.**

So the standard objection to a generous free tier — "it is so complete nobody
reaches the paywall" — is real but mispriced. Someone who installs, loves the VCP
screener and never pays costs approximately nothing. Meanwhile the upside is a
genuinely differentiated free product: 37 quant tools no other MCP server offers,
against a niche where the top result for `options mcp` has **39 stars**.

The line is also honest rather than crippled. "Nightly research free, intraday
positioning paid" is a distinction a trader understands immediately, and it does
not feel like a demo with the good parts sawn off.

**The one design requirement it creates:** those two halves serve different jobs.
Someone who came for a nightly screener may never want intraday gamma. So the
free tools must *surface the gap* — a screener result that ends "3 of these have
unusual flow today" and stops — rather than quietly not mentioning it. Otherwise
the free product is complete and the paid one is invisible. This is a small
amount of work in tool output and it is the highest-leverage item in the plan.

---

## 2. What actually exists — four MCP surfaces

| # | surface | tools | auth | reachable by |
|---|---|---|---|---|
| 1 | `trading_mcp` — `agent.mphinance.com` | 60 (47 momentum + 13 Vesper reads) | bearer + OAuth 2.1 | you only |
| 2 | `mcp_server/server.py` — local stdio | ~47 momentum | none (local) | you only |
| 3 | `/api/v1/mcp` — TMpro backend | 18 tool modules | `td_live_` API key | anyone with a key |
| 4 | `tools/td-mcp.mjs` — Vespryx | 17 (5 dealer + 12 `tv_*`) | session JWT | a subscriber who clones the repo |

**They barely overlap.** Surfaces 1 and 2 are the quant/analytics layer plus
Vesper state. Surface 3 is TMpro's flow layer. Surface 4 is one account's live
gamma view plus chart control. Only the TDPro passthrough is duplicated — and
that was the part that was both broken and unshippable until 2026-09-03 (§3, §7).

**Neither apex levels nor conviction is served by any of them** except through a
browser session. Four MCP servers, and not one can deliver the two best assets.

---

## 3. The live breakage — FIXED 2026-09-03

Half the deployed surface was dead. Three real calls to `agent.mphinance.com`
found it:

```
get_market_pulse   →  {"error": "TRADERDADDY_API_URL not set in .env"}
get_fundamentals   →  AAPL, $328.21, mkt cap 4.79T          ✅
get_halt_status    →  {"available": true, "is_halted": false} ✅
```

Every TDPro-backed tool, across four modules (`server.py`, `macro.py`,
`pead_screener.py`, `registry.py`). Cause and fix are §7 — it was never a
missing env var.

**Fixed and deployed** (`8bb1dcb`). Re-verified on the live server after restart:

```
get_market_pulse      ✅ live data
get_market_stats      ✅ live data
get_unusual_activity  ✅ live data
get_gex_overview      ✅ live data
```

Worth keeping in mind for §1: while it was broken, the box was running **~37
working tools with the entire TMpro half erroring** — which is the free tier,
accidentally, in production, holding up fine without TMpro at all.

---

## 4. The paid side, and what it can actually be sold through

### Vespryx is the distribution that already exists

`tradernetwork/dealer-hud`, v0.22.12, **live on the Chrome Web Store**:
`https://chromewebstore.google.com/detail/vespryx/pogpghbkbbbjolibaeaefnfklhgchkob`

It is a paid product with its own subscription (`hasVespryx`, explicitly separate
from TD Pro), trader-facing, riding the user's own session — so it reaches apex,
which no API key can. One-click install, real search, install counts. No GitHub
repo competes with that for a trader audience.

### The free/paid line already exists in the data layer

- `GET /gex/{sym}` and `GET /ticker/{sym}` need **no Authorization header at all**
- `GET /gex/{sym}/apex` requires the session and returns
  `200 {locked:true, error:'premium_required'}` when unentitled

That is degradation rather than a wall, already built. GEX being public is a
gift: the free tier gets a genuinely good hook that costs nothing to give away.

### What the paid key surface can and cannot serve

`/api/v1/mcp` serves 18 modules — gex, unusual activity, dark pool, directional
flow, smart money, hedge, short data, premarket gappers, ticker lab, screener,
sectors, put/call, signals, earnings, economic calendar, insider, long term,
market stats.

**It does not serve apex or conviction.** Their own
`reference/data-licensing.md` §3 classifies both as 🟢 derived TDP IP and calls
`get_apex_levels` *"the showcase for a derived-only license"* — so publishing them
is permitted, just never built. No public tool module exists for either, on any
branch. The pattern to copy exists 18 times over in `src/mcp/public/tools/`, each
with a projection allowlist and a test.

---

## 5. What has to be built

Four items, in dependency order. Only the first is strictly required for the free
funnel; the rest are what turn it into revenue.

### 5.1 Let free-tier users mint a key — not a new tier

**A free tier already exists.** `src/middleware/auth.ts:181-184`:

```js
const tier = req.user.subscription_tier ?? 'free';
const tierOrder = { free: 0, premium: 2 };
```

The comment above it says there is exactly one paying tier. So every signed-up
non-payer is already `free`. Nothing to model, no pricing change.

The gap is a *different switch*: `has_api_access`, which gates minting a
`td_live_` key, is flipped true **only** by the Stripe webhook on a completed
subscription — and the 5-day trial uses `missing_payment_method: 'cancel'`, so a
card is required up front. A free user can log in and cannot get a key at all.

**🔴 Do NOT just flip the flag.** An earlier revision of this plan called it
"one rule, not a pricing change". That was wrong, and dangerously so.

`src/middleware/apiKeyAuth.ts:96-97` stamps **every** API-key caller:

```ts
is_premium: true,
subscription_tier: 'premium' as const,
```

Hardcoded, regardless of what the user's row actually says. Their own code
comments on the consequence at `src/routes/publicApi.ts:145-147`:

> *"Its `requireTier('premium')` gate is NOT a defence on this surface: every
> `td_live_` caller is stamped `subscription_tier: 'premium'` by
> middleware/apiKeyAuth.ts, so the tier check passes unconditionally here."*

So the moment a free user gets `has_api_access = true`, they hold a key that
passes **every** `requireTier('premium')` gate in the application — not the
reduced surface this plan wants, the entire product. §5.2 is therefore not a
follow-up to §5.1; it is a **prerequisite**.

Confirmed alongside it:

- **Only Stripe writes `has_api_access`** — four handlers in
  `src/routes/subscriptions.ts` (`:2904`, `:3365`, `:3489`, `:3624`). No admin
  or comp route touches it; comping is done by hand in the database.
- **The mint path checks nothing else.** `src/routes/developer.ts:349-350` reads
  `has_api_access` and that is the entire guard, plus a 5-key cap. It never
  looks at `subscription_tier`.
- **`rate_limit_per_min` is a column default of 30**, never written by
  `generateKey` — so a lower free-tier limit means passing an explicit value in
  the INSERT (`apiKeyService.ts:62-67`). No migration required.
- **A manual grant is never auto-revoked.** The revocation cascade in
  `handleSubscriptionDeleted` keys off a matching Stripe subscription id; a
  hand-granted key has none, so removal is also manual.
- **Analytics will misreport it.** Free traffic logs as `tier: 'premium'`
  (`services/screeners/runLog.ts:31-39`), and `adminComped.ts:53` filters
  `WHERE subscription_tier <> 'free'` — so the one admin view built to audit
  "who has access without paying" cannot see these users.

### 5.2 Fix the premium stamp, then add scope — prerequisite, not follow-up

Two changes, in this order:

1. **Stop hardcoding premium.** `apiKeyAuth.ts:96-97` should carry the real
   `subscription_tier` — which `apiKeyService.validateKey` already fetches via
   its JOIN (`apiKeyService.ts:82-88`) and then discards. The values are right
   there; they are simply overwritten with a literal. **Auditing every
   `requireTier('premium')` route is part of this change**, because today they
   are all passing unconditionally for key callers and nobody has been relying
   on them working.
2. **Add a `scope` / `allowed_tools` column**, checked in `finishApiKeyAuth`
   between the `has_api_access` check (`:63`) and the rate-limit step (`:71`).
   The `api_keys` table (DDL: `database/migrations/142_developer_api_access.sql`)
   has `id, user_id, name, key_prefix, key_hash, rate_limit_per_min, is_active,
   last_used_at, created_at, revoked_at` — no scope, no tier, no expiry.

Migrations are numbered `.sql` files under `database/migrations/`, applied by
hand with `node database/run-migration.js <file>.sql`. No framework, no
tracking table — so a migration is a deliberate, manual act here.

### 5.3 A device flow

Vespryx's headless path works but the credential handoff is manual: the user
clicks "copy desktop token", pastes `{access_token, refresh_token}` into
`td-token.mjs`, and it lands in `~/.cache/vespryx/td-token.json` (0600).

That has already failed expensively. `td-api.mjs:296-303` records it: **the
session had been dead since 4 August and every tool answered "Dealer levels are a
paid feature" on a paid-up plan** — because apex returns `200 {locked:true}` for
an expired token *and* for a genuine entitlement failure. The code now separates
`SESSION_EXPIRED` from `LOCKED`, but the root cause is a credential that rots
because a human has to re-paste it.

The flow, same shape as `gh auth login`: tool prints a URL and short code → user
approves in the browser where they are already signed in → backend checks the
entitlement and mints → tool polls, receives, writes the token file.

**It is also the purchase moment.** A user without an entitlement gets a signup
link with a referral parameter *in the same response*, so the buy decision happens
where they already are rather than five context switches away on a pricing page.

### 5.4 Apex and conviction as public tool modules

Optional and last, but it is what a paid key is actually *for*. Until this exists,
a paid key buys the flow layer — good, but not the differentiators.

---

## 6. The public repo already exists — `mphinance/momentum-mcp`

**Do not create `quant-mcp`.** `mphinance/momentum-mcp` is already public, and:

| | |
|---|---|
| stars / forks | **26 / 8** |
| description | *"⚡ Give your AI agent a Bloomberg terminal. MCP server for stock screening, OHLCV data, technical analysis, chart generation, and financial news."* |
| last pushed | 2026-07-22 |
| contents | the pre-M0-split ancestor of this repo's `mcp_server/` |

26 stars is not nothing in a niche where the top result for `options mcp` has
**39** and for `quant mcp` has 120. Starting a new repo at zero would be moving
*backwards* to gain a better name — and the name was never going to carry this
(§1).

**Nothing is missing from it.** The 15 modules it has that this repo appears to
"lack" — `cache`, `charts`, `conviction`, `data`, `knowledge`, `macro_regime`,
`market_top`, `options`, `options_greeks`, `risk`, `schema`, `screener`,
`technicals`, `traderdaddy`, `vcp_screener` — are exactly the ones M0 relocated
to `core/`. This repo is ahead by that split plus `edgar_tools.py`,
`registry.py`, and everything added since July.

So the work is **update it from here**, not migrate to somewhere new:

1. Bring it up to date with this repo's `mcp_server/` + the 16 `core/` modules
   (the manifest below), preserving its stars, forks and inbound links.
2. Rewrite its README around the free/paid split in §1.
3. Publish `momentum-mcp` to PyPI so `uvx momentum-mcp` works.
4. Reserving `quant-mcp` on PyPI/npm is now optional — cheap insurance against
   someone else taking it, not a plan.

The naming question that consumed a chunk of revision 3 is therefore closed by
circumstance: the repo with traction wins, and it is already named.

### What ships publicly

The cut is already drawn and mechanically enforced: `mcp_server/`'s full
transitive closure — including deferred and function-level imports — reaches
`vesper/` and `trading_mcp/` **zero times**, pinned by
`tests/test_import_boundaries.py::test_mcp_server_never_imports_vesper`.

**Ships:** `mcp_server/` (28 files) + 16 `core/` modules: `cache`, `charts`,
`conviction`, `data`, `edgar`, `knowledge`, `macro_regime`, `market_top`,
`options`, `options_greeks`, `risk`, `schema`, `screener`, `technicals`,
`traderdaddy`\*, `vcp_screener`.

**Never ships:** `vesper/`, `trading_mcp/`, `deploy/`, `autonomous/`, `docs/`, and
`core/`: `wb`, `md`, `td`, `approval_registry`, `halt`, `circuit_breaker`,
`paper_ledger`, `audit_chain`, `position_preview`, `metrics`, `quotes`,
`secret_hygiene`.

No broker client is in the public graph — `core/wb.py` and `core/md.py` are both
absent, so nobody can point it at a brokerage.

\* *`core/traderdaddy.py` ships only after §7 is done. As written it must not.*

---

## 7. ~~The blocker~~ — DONE (`8bb1dcb`, 2026-09-03)

*Kept as the record of what it was and why it mattered. The fix is deployed and
verified; §3 has the after.*

`core/traderdaddy.py:207`:

```python
url = f"{base}/api/agent/{path.lstrip('/')}"
```

`/api/agent/*` is the **internal** namespace — gated by `AGENT_API_KEY`, a single
shared master credential, reserved for MCP-internal, the Discord bot and in-app
chat. It is not the customer API. And `_agent_get` calls `_get_token()`
regardless, which needs `TRADERDADDY_EMAIL` + `TRADERDADDY_PASSWORD`.

Three consequences:

1. **§3 is not a one-variable fix.** Setting `TRADERDADDY_API_URL` alone leaves
   `_get_token()` failing. And on an OAuth-only account there may be no password
   to supply at all.
2. **This file cannot ship publicly as written.** A public repo containing a
   client for your internal superuser namespace is a different kind of mistake
   from a leaked hostname.
3. **The fix is the same as the funnel work.** Repoint at `/api/v1/*` with an
   `X-API-Key` header, delete `_login()`/`_get_token()`.

That last point is why this is cheap. `src/routes/publicApi.ts:60-77` re-exports
**the same route handlers** at `/api/v1/*` that the web app uses at `/api/*` —
same handlers, same response shapes. So the ten call sites in
`mcp_server/server.py` do not change at all; only `_agent_get` does. Roughly a
20-line diff that simultaneously fixes the live breakage, removes the master-key
dependency, makes your own server use the credential a customer would, and makes
the file shippable.

**Done.** `core/traderdaddy.py` now calls `/api/v1/<path>` with an `X-API-Key`
header off `TD_API_KEY`; the JWT login path is deleted; the default host is
`https://api.traderdaddy.pro` so a missing env var can never again mean total
failure; and `alerts*` / `most-institutionally-traded-tickers` explain themselves
rather than 404. Eleven tests pin the URL on the wire and the credential in the
header (`tests/test_traderdaddy_public_api.py`) — a regression there re-points
the public tool surface at an internal namespace, which is not a style problem.

---

## 8. Sequence

| # | step | gate |
|---|---|---|
| ~~1~~ | ~~Repoint `core/traderdaddy.py` at `/api/v1/*`~~ **DONE `8bb1dcb`** | ✅ verified live |
| ~~2~~ | ~~Can `has_api_access` be set without Stripe?~~ **ANSWERED** — only 4 Stripe handlers write it; comping is a manual DB write | ✅ |
| 3 | **Stop hardcoding `subscription_tier: 'premium'`** in apiKeyAuth.ts:96-97, and audit every `requireTier('premium')` route that has been passing unconditionally because of it (§5.2) | a free-tier key is refused where premium is required |
| 4 | Add the `scope`/`allowed_tools` column + check (§5.2) | a scoped key is refused outside its scope |
| 5 | Only now: let `free` tier mint a capped key (§5.1) | a carded-out stranger gets a working, *limited* key |
| ~~6~~ | ~~Make the free tools surface the gap~~ **DONE `4b804a8`** ("3 of these have unusual flow today") | the paid layer is visible from inside the free one |
| 7 | Device flow, with the signup link in the un-entitled response (§5.3) | no copy-paste; buy happens in-flow |
| 8 | Fix `QUICKSTART.md` Path 2 — it tells paid users to clone a **private** repo (404) | link resolves |
| 9 | Update **`mphinance/momentum-mcp`** from this repo (§6); README around the free/paid split; `server.json`; publish to PyPI | `uvx momentum-mcp` works from a clean machine |
| 10 | Instrument: installs, gate-hits, attributed signups. 90-day checkpoint | a number written down beforehand |
| 11 | *Later:* apex + conviction public tool modules (§5.4) | projection allowlist + guard test |

Steps 1 and 7 are worth doing whatever happens to the funnel.

---

## 9. Open questions

1. **Is `has_api_access` writable outside the Stripe webhook?** Decides whether
   §5.1 is a config change or a schema change.
2. **Does anything in Vespryx go public?** QUICKSTART Path 2 already assumes it
   is, and it is the product being sold.
3. **What is the free key's rate limit?** Paid is 30/min. Free needs to be
   usable-but-clearly-less.
4. **Does the free MCP server ship the knowledge base?** `data/chromadb/` is 16 MB
   and gitignored, so the code would ship with no data behind it. Seed, exclude,
   or document as bring-your-own.

---

## 10. Change log

| earlier revisions said | revision 4 |
|---|---|
| Build a public MCP repo as the funnel (r1-r2) | The 37 zero-cost tools are the funnel; Vespryx is the paid distribution |
| The free layer is too complete, it will leak (r2) | Mispriced. It costs nothing to serve — the leak is free |
| The funnel is Vespryx, `quant-mcp` is optional (r3) | Both: free tools acquire, Vespryx and the key surface monetise |
| "needs a TMpro API key" (r2) | Keys are real, per-customer, revocable, rate-limited — and already gate `/api/v1/mcp` |
| Add a free tier (r3) | A free tier **already exists**. The gap is `has_api_access`, one rule |
| One env var fixes the live breakage | False. `traderdaddy.py` calls `/api/agent/*`, the internal superuser namespace |
| Ship `core/traderdaddy.py` in the public manifest | Not until it is repointed at `/api/v1/*` |
| Apex/conviction reachable by key | Neither is on any key surface. Session-only |
| Create `quant-mcp` as the public repo | **`mphinance/momentum-mcp` already exists** — public, 26 stars, 8 forks. Update it; do not restart at zero |
| `core/traderdaddy.py` is the blocker (§7) | Fixed and deployed 2026-09-03 (`8bb1dcb`); the live server's TMpro half works again |
| Free-tier keys are "one rule, not a pricing change" | **Wrong and dangerous.** `apiKeyAuth.ts:96-97` hardcodes `subscription_tier: 'premium'` for every key caller, so flipping the flag grants the whole product. Scope work is a prerequisite, not a follow-up |

**Verified and unchanged:** the 16-module manifest and its zero `vesper`
reachability; no broker client in the public graph; `td_live_` keys hashed,
revocable, Redis rate-limited, failing closed; GEX public and apex gated;
`quant-mcp` free on PyPI and npm; the niche ceiling (`options mcp` peaks at 39
stars).
