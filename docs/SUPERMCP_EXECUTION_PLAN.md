# supermcp execution plan — get the MCP side up, trading owner-only (2026-09-01)

Scope Michael signed off on: **stand up the MCP side on supermcp, with trading reachable
only by his own key.** Subscribers keep `michaeliscool` and can mint viewer keys exactly
as they do today. Voice-approves-orders (Phase 5 of `SUPERMCP_CONSOLIDATION_PLAN.md`) is
**not** in scope and is not being built.

## Current state

| Location | HEAD | Tree | Notes |
|---|---|---|---|
| `origin/master` | `9d367fe` | — | pre-RBAC; nothing pushed since |
| `coolify:~/supermcp` | `5f52f11` (nyx) | `8c916d5` | dev clone, **no `.venv`** — cannot run or test |
| `vultr:/home/mphinance/supermcp` | `565a857` (mphinance) | `6106ad6` | **LIVE**, has the venv, serving `mcp.mphinance.com` |

Both RBAC commits share parent `9d367fe`, were authored 3s apart, and differ in content
(coolify's also rewrote `CLAUDE.md`). Neither box has the other's object. **This must be
reconciled before any new code lands** or the next `git pull` on prod merges into a
running trading server.

Live exposures, in severity order:

1. **`POST /login` exchanges `michaeliscool` for the admin token.** `TOOLS_PASSWORD` is
   unset in prod `.env`, so it falls back to the `config.py:41` hardcoded default.
   Post-RBAC that token carries `['read','trade','admin']` → the shared subscriber
   password is currently a live path to placing Webull orders.
2. **`GatedOAuthProvider.authorize()` force-adds `['read','trade','admin']` to every
   authorization**, unconditionally. Whether that's reachable without the password gate
   is unverified and is the second thing to check.
3. **`UNGATED_ORDER_BROKERS=webull`** still waives `LIVE_ORDERS_ENABLED` and the
   `"SEND IT LIVE"` confirm phrase, with $10k/1,000-share caps. Out of scope for this
   pass but stays on the record — narrowing *who* holds a trade-scoped key does not
   restore the caps on what that key can then do.

## Workstreams

### W0 — Reconcile the fork (blocking, Michael's assistant does this, not an agent)

Take coolify's `5f52f11` as canonical (it's the superset — same code plus the `CLAUDE.md`
update), push to origin, fast-forward vultr onto it. Prod has one uncommitted-file
concern only (`webull_trade_sdk.log.*` — untracked logs, safe). Verify the running
service is unchanged afterward: `systemctl status`, then a bearer-authed `/api/holdings`
curl. Nothing else starts until this is clean.

### W1 — Auth trade-scope lockdown ✅ LANDED 2026-09-01 ~19:50 UTC

Deployed to vultr as one verified combined patch (`git apply --check` exit 0 against a
pristine pull, reverse-apply verified for rollback). `SUPERMCP_VIEWER_TOKEN` set in prod
`.env` first, then the patch, then `systemctl restart supermcp` — clean startup, no
traceback. Verified: `michaeliscool` → `/login` returns the viewer token, not the master.

Landed: the `/login` admin/viewer split (fails closed to 503 if the viewer token is
unset); `accounts.py` `current_user()` terminal fallback OWNER → ANONYMOUS, closing the
personal-holdings leak to cookie-only subscribers; `hmac.compare_digest` on all four token
comparisons; `require_trade_scope()` fails closed; `GatedOAuthProvider.authorize()` no
longer force-adds admin to every grant.

Known follow-ups, not regressions: the dashboard's "Reader SSO" mode means any browser
without an explicit `/login` (no `localStorage.supermcp_token`) now resolves the owner to
ANONYMOUS — re-run `/login` per browser. `UNGATED_ORDER_BROKERS=webull` is still set
(inert — no 2FA'd Webull session on that box — but should be emptied). The claude.ai
connector still carries the master token; a `--role trader` minted key is the replacement.

Original scope below, kept for reference.

### W1 (original) — Auth trade-scope lockdown (safety-critical, lands first)

Per `docs/AUTH_TRADE_SCOPE_LOCKDOWN.md`. `michaeliscool` **stays** — the fix is that
`/login` must stop handing back a trade-capable token for it.

- Split `/login`: `TOOLS_PASSWORD` match → return `SUPERMCP_VIEWER_TOKEN` (read-only);
  `OAUTH_PASSWORD` / raw `SUPERMCP_TOKEN` match → return the admin token.
- Set a real `SUPERMCP_VIEWER_TOKEN` in prod `.env`.
- Audit every other path that returns or compares `SUPERMCP_TOKEN` for the same bug.
- Verify no non-admin caller can mint or obtain a `trade`/`admin`-scoped credential,
  including via the OAuth `authorize()` scope-injection above.

**Acceptance:** `michaeliscool` → `/login` returns the viewer token; that token gets
`permission_denied` from `place_live_order`; the dashboard still fully works for
subscribers; Michael's own bearer still trades.

### W2 — Mount the momentum tools (the "MCP parts")

`mcp_server/registry.py` + `dist/supermcp_momentum_tools.tar.gz` are already built here.
Deploy Tier 1 first (19 tools, zero heavy deps), then Tiers 2–3 once the dependency delta
is known. **Tier 4 (chromadb) stays local** — too heavy for supermcp's deliberately-thin
tree.

Open question the agents answer before anything is mounted: **tool-name collisions**
between the 47 momentum tools and supermcp's existing 37. FastMCP will either error or
silently shadow; neither is acceptable on a live connector.

**Acceptance:** service restarts clean, `journalctl` shows no import errors, tool count
rises by exactly the expected number, and the new tools are read-only (no order path).

### W3 — Swagger/OpenAPI (lowest priority, additive)

Per `docs/SUPERMCP_SWAGGER_AND_AUTH_PLAN.md`. Note that plan puts `/openapi.json` and
`/docs/` **outside** `BearerAuth`'s `/api`-only guard, i.e. public. Decide deliberately
whether the endpoint inventory should be public before shipping it — documenting
`/api/order/execute` to anonymous readers is a choice, not a detail.

## Division of labour

- **Sonnet agents (parallel, read-only):** audit the auth surface, the scope-elevation
  paths, the tool-name collisions, the dependency delta, and the fork diff. They produce
  findings and exact proposed patches — they do **not** touch production.
- **Sonnet agent (sequential):** author the patches against the reconciled tree.
- **Adversarial review agent:** check the patches actually close the hole without
  breaking subscriber read access.
- **Michael's assistant (me):** the fork reconciliation, and every write to the live
  server + `systemctl restart`. A live brokerage host is not somewhere an autonomous
  agent gets to restart a service unattended.

## Rollback

Every prod change is one commit. If a restart comes back unhealthy: `git revert HEAD`,
`systemctl restart supermcp`, wait ~7s (uvicorn bind delay — a fast curl returns 000/503
mid-restart and that's timing, not failure), re-curl `/api/holdings`.
