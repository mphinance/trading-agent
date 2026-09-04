# Gotchas across the estate

Things that cost real time, found the hard way, mostly on 2026-09-03. Each one
is here because it was **not** discoverable by reading the code casually — it
either contradicted a comment, hid behind a plausible-looking success, or lived
in a different repo from the thing it broke.

Systems referenced:

| name | what | repo |
|---|---|---|
| **Vesper** | the trading agent, order path, `agent.mphinance.com` | `mphinance/trading-agent` |
| **momentum-mcp** | the public quant MCP server (26★) | `mphinance/momentum-mcp` |
| **TMpro** | TraderDaddy / TraderMatrix Pro backend + API | `avorojeykin/TraderDaddy-Pro---Whop` |
| **Vespryx** | Chrome extension + desktop tools | `tradernetwork/dealer-hud` |
| **supermcp** | the vultr MCP host | `supermcp` (private) |

---

## Credentials and secrets

**A placeholder became a live production credential.** `trading-agent.service`
served the public internet for a day with `TRADING_AGENT_TOKEN` set to
`your_strong_random_secret_token` — the literal value from
`.env.trading-agent.example`, committed to a public repo. Everything looked
healthy: service up, TLS valid, tests green, unauthenticated requests correctly
refused. The gate was simply published. *Guards now: `core/secret_hygiene.py`
refuses to open a listener with a placeholder; `deploy/install.sh` generates a
real token and refuses to deploy any credential still equal to its example.*

**Three env files, one of them read by nothing.** On a deploy box:

| file | read by |
|---|---|
| `~/trading-agent/.env.trading-agent` | `trading-agent.service` (`EnvironmentFile=`) |
| `~/trading-agent/.env.vesper` | `vesper-loop`, `vesper-listen` |
| `~/trading-agent/.env` | **nothing** — but `load_dotenv()` finds it, so it silently fills gaps |

Rotating a credential in `.env` changes nothing the service reads. This is how
the incident above went unnoticed: the real-looking token was in the file nobody
loads.

**A credential scanner only knows the prefixes it was born with.** CI scanned for
`sk-ant-`, `td_live_` and Telegram bot tokens. A full `read,trade`-scoped
supermcp key (`smk_…`) sat in an untracked doc for two days, invisible, one
`git add .` from a public repo. *Guard now: `scripts/scan_secrets.sh` is the one
pattern list, called by both CI and `.githooks/pre-commit`. When a new
credential format appears anywhere in the estate, add it there.*

**A placeholder allowlist is worse than no allowlist.**
`.env.vesper.example` shipped an uncommented
`TELEGRAM_AUTHORIZED_USER_IDS=12345678,87654321`. Copied verbatim that is a
*working* allowlist: the set is non-empty so the "unset — anyone can approve"
warning never fires, **you** cannot approve your own trades, and the only
accounts that can are two IDs published in the repo. *Guard: fails closed now.*

**A GET form puts the secret in the access log.** `trading_mcp`'s OAuth operator
gate used `method="get"`, so `operator_key=<TRADING_AGENT_TOKEN>` went into the
request line on every ordinary reconnect — kept verbatim by Traefik's and
uvicorn's logs and the browser's history. No attacker required.

---

## TMpro backend — the ones with teeth

**`requireTier('premium')` is not a defence on the API-key surface.**
`src/middleware/apiKeyAuth.ts:96-97` hardcodes `is_premium: true` and
`subscription_tier: 'premium' as const` for **every** `td_live_` caller,
regardless of the user's real row — which `validateKey` fetches via its JOIN and
then discards. Their own comment at `src/routes/publicApi.ts:145-147` says so:
*"the tier check passes unconditionally here."*

**Consequence:** granting `has_api_access` to a free user does not give them a
reduced surface. It gives them every premium-gated route in the application.

**`has_api_access` and `subscription_tier` are unrelated switches.** A free-tier
user exists, can log in, and cannot mint a key at all. Only four Stripe webhook
handlers in `src/routes/subscriptions.ts` ever write `has_api_access`
(`:2904`, `:3365`, `:3489`, `:3624`) — no admin route does; comping is a manual
DB write. And `src/routes/developer.ts:349-350` checks *only* `has_api_access`
before minting; it never reads the tier.

**A hand-granted key is never auto-revoked.** The revocation cascade in
`handleSubscriptionDeleted` keys off a matching Stripe subscription id. A key
granted manually has none, so nothing will ever turn it off.

**Free-tier usage would be invisible to the audit view built for it.**
`adminComped.ts:53` filters `WHERE subscription_tier <> 'free'`, and
`services/screeners/runLog.ts:31-39` stamps public-API traffic as
`tier: 'premium'` in analytics regardless of the truth.

**`rate_limit_per_min` is a column default of 30 that nothing overrides.**
`apiKeyService.generateKey` INSERTs without naming it. A different limit needs an
explicit value in the INSERT — not a migration; the column already exists.

**Migrations are hand-run.** Numbered `.sql` files under
`database/migrations/`, applied with `node database/run-migration.js <file>.sql`.
No framework, no tracking table. A migration is a deliberate manual act.

**Two namespaces, same handlers.** `src/routes/publicApi.ts:60-77` re-exports the
*same* route handlers at `/api/v1/*` (API-key gated) that the web app uses at
`/api/*` (JWT). Same responses, different credential — which is why repointing a
client between them changed no call sites.

**`/api/agent/*` is the internal superuser namespace.** Gated by `AGENT_API_KEY`,
a single shared master credential, hard-scoped to that prefix. `core/traderdaddy.py`
used to call it, which meant the module could never ship publicly and needed an
email/password login that an OAuth-only account cannot supply. *Fixed 2026-09-03
(`8bb1dcb`).*

**GEX is already public.** `GET /gex/{sym}` and `GET /ticker/{sym}` need **no
Authorization header at all**. `GET /gex/{sym}/apex` is the gated one, returning
`200 {locked:true, error:'premium_required'}`. The free/paid line already exists
in the data layer, and it is degradation rather than a wall.

**Apex and conviction are not on any API-key surface.** Neither has a public tool
module, on any branch — despite `reference/data-licensing.md` §3 classifying both
as 🟢 derived TDP IP and calling `get_apex_levels` *"the showcase for a
derived-only license."* Permitted, never built.

---

## Vespryx

**A dead session is indistinguishable from an unpaid one.**
`tools/td-api.mjs:296-303` records the incident: **the session had been dead
since 4 August and every tool answered "Dealer levels are a paid feature" on a
paid-up plan.** Apex returns `200 {locked:true}` for an expired token *and* for a
genuine entitlement failure. The code now separates `SESSION_EXPIRED` from
`LOCKED`, but the root cause is a credential a human has to re-paste by hand.

**`td-mcp.mjs` never ships in the extension.** `tools/package.mjs:348-353`
explicitly excludes `tools/`, `test/`, `site/`, `docs/` and `pine/` from the
Chrome Web Store zip, with a hard leak check that exits non-zero if any slip in.

**…but QUICKSTART tells paying users to clone a private repo.**
`QUICKSTART.md:27-60` ("Path 2: Desktop CDP Automation & Coding Agents") is
published, `beginner+`-tier-gated instruction to run
`git clone https://github.com/tradernetwork/dealer-hud`. That repo is
**private** — every non-collaborator gets a 404 on a feature they paid for, and
the clone path is the *only* route to the MCP server. **Still open.**

**A browser session does not transfer to a headless agent.** The extension reads
a Supabase JWT from `localStorage` via a content script on `traderdaddy.pro`.
None of that exists on an MCP server running on a stranger's laptop. The desktop
bridge is a *manual* copy-token-and-paste into `~/.cache/vespryx/td-token.json`.

---

## Vesper / this repo

**Bare module-level imports of heavy deps make the whole server unstartable.**
`mcp_server/server.py` imports all 47 tools at module scope, and
`core/knowledge.py` (`import chromadb`) plus `mcp_server/backtest.py`
(`import matplotlib`) had no guard. Dropping chromadb — the obvious move for a
light install, it is ~178 MB — raised `ModuleNotFoundError` before a single tool
registered. It did not degrade one tool; it killed the process. *Fixed.*

**The knowledge base ships code with no data.** `data/chromadb/` is 16 MB and
gitignored, so `search_knowledge` returns nothing on a fresh install.

**M8's tools are written, tested, and never registered.**
`trading_mcp/server.py`'s `_register_all_tools()` does not call
`register_order_tools` / `register_voice_tools` / `register_drafting_tools`, so
`halt`, `watch_setup`, `draft_proposal` and `place_order` are unreachable — while
`SERVER_INSTRUCTIONS` and the `copilot_setup` prompt actively tell clients to
call them. The advertised emergency halt does not exist on the wire.

**And wiring them in would lock you out, not open a hole.**
`_build_oauth_provider` passes `required_scopes=["read"]`, which the constructor
reuses as `valid_scopes` — collapsing it to `{"read"}`. No credential the server
can issue would satisfy `require_scopes("trade")`. Fail-closed, and pinned by
`test_production_oauth_provider_scope_plumbing`.

**An AST pin that matches only `Call` nodes is defeatable.** The rule-3 guard
pin missed `getattr(guard, "place")` — dynamic dispatch by string produces no
literal attribute node. It also originally missed `asyncio.to_thread(guard.place, …)`,
which is the idiom the live order path actually uses. *Both closed; don't narrow
it back.*

**`get_account_state` constructs a fresh `Webull()` per call.** The TTL cache,
lock and stale-fallback live on the *instance*, so every MCP call is a guaranteed
cache miss that spends the entire 2-req/2s account-query budget — the same bucket
`vesper-loop` needs for its -40% 0DTE stop. **Still open.**

**`mcp_server/constellation.py` is dead code.** Imported by nothing, sole user of
`litellm`, reads `OPENROUTER_API_KEY`. Excluded from the public manifest.

**Asserting a file *count* is a bad tripwire.** `test_public_export` had
`len(files) == 44`. It fired correctly the first time a module was added and
reported only that a number had moved. Compare **names** against a reviewed
baseline instead, so the failure says which file would become world-readable.

**`momentum-mcp` is the pre-M0-split ancestor of `mcp_server/`.** The 15 modules
it appears to have that this repo lacks are exactly the ones M0 relocated into
`core/`. Nothing is missing; it is behind, not different.

---

## Infrastructure

**Wildcard DNS means a typo never fails.** `*.mphinance.com` → vultr, so a
mistyped hostname silently lands on the wrong machine and returns a confusing
503 rather than NXDOMAIN.

**Traefik cannot reach the host's loopback.** It is containerised, so
`trading-agent.service` binds the docker bridge `10.0.0.1:8500` — not loopback,
not `0.0.0.0`. `MCP_HOST` defaults to `127.0.0.1` so reaching the bridge is
always explicit.

**Rotating `TRADING_AGENT_TOKEN` breaks the connector silently.** The claude.ai
connector reports "Couldn't reach…", which reads like a network or URL problem
and is actually a 401. Because the same secret signs OAuth tokens, the fix is to
**remove and re-add** the connector, not edit it in place.

**An `export` in a shell does not reach a systemd service.** It gets its own
environment from `EnvironmentFile=`.
