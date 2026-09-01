# Lock trade/Webull scope to Michael's key only (2026-09-01)

**Constraint, not a bug to "fix" by changing it:** `TOOLS_PASSWORD` (currently defaulting
to `michaeliscool` in `src/config.py`) is the password clients/subscribers already know
and use to reach the dashboard and read-only tools. **Do not rotate or remove it** —
that breaks access for people who already have it. The problem isn't that this password
exists or is known; it's that today's RBAC commit made a route that accepts it hand back
a token with `trade`/`admin` scope. Fix the *routing*, not the password.

## The bug

`POST /login` (`src/app.py:833`) does this:

```python
ok = bool(pw and pw in (config.TOOLS_PASSWORD, config.OAUTH_PASSWORD, config.SUPERMCP_TOKEN))
...
return JSONResponse({"token": config.SUPERMCP_TOKEN})   # <- always the ADMIN token
```

Since the newly-landed RBAC commit (`feat: implement token-based RBAC and trade scope
enforcement`, `565a857`) gives `SUPERMCP_TOKEN` `['read', 'trade', 'admin']` scope,
**anyone who knows `michaeliscool` can now redeem it for a token that can place live
Webull orders.** That's the entire finding — the shared password reaching a
trade-capable credential, not the password being shared in the first place.

## What "done" looks like

- Everyone who knows `michaeliscool` can still log into the dashboard and use every
  read-only tool exactly as today. No behavior change for subscribers on the read side.
- **Only Michael's own credential** ever carries `trade` scope, and therefore only his
  session can ever reach `place_live_order` / `dry_run_order` / the Webull broker.
- Self-service key minting (`mcp_keys.py`, and the swagger doc's proposed
  `/api/admin/keys/generate`) keeps working for everyone — but every self-minted or
  password-redeemed key defaults to `viewer` (`['read']`) and there is **no path,
  public or otherwise, for a non-admin caller to end up with `trade` or `admin` scope.**

## The fix

1. **`/login` must not hand back `config.SUPERMCP_TOKEN` for a `TOOLS_PASSWORD` match.**
   Split the branches:
   - `pw == TOOLS_PASSWORD` → return a **viewer-scoped** token. `SUPERMCP_VIEWER_TOKEN`
     already exists as a config knob from today's commit — set it in `.env` to a real
     value and return *that* here, not the master token.
   - `pw == OAUTH_PASSWORD` or `pw == SUPERMCP_TOKEN` (i.e. the caller already holds or
     knows the real owner secret, not the shared dashboard password) → this is Michael,
     return the admin token as today.
2. **Set `SUPERMCP_VIEWER_TOKEN` in `.env` on `vultr`.** Right now it's an unset,
   optional knob — `/login`'s viewer branch needs something real to hand back.
3. **Audit every other place `config.SUPERMCP_TOKEN` gets returned or compared for a
   password-based (not owner-secret-based) match** — the same class of bug could exist
   anywhere else that treats "knows the dashboard password" as equivalent to "holds the
   admin token." Grep for `SUPERMCP_TOKEN` usage across `src/app.py` and `src/auth.py`
   and check each one's trust boundary, not just `/login`.
4. **Confirm `mcp_keys.py` gives no caller-reachable way to mint above `viewer`.** The
   CLI (`python -m src.mcp_keys mint <label> --role trader`) is fine — it's operator-run,
   not client-facing. If/when the swagger doc's `/api/admin/keys/generate` HTTP endpoint
   gets built, it must (a) require `SUPERMCP_ADMIN_TOKEN` specifically — not just any
   `admin`-scoped token if that ever diverges — and (b) hardcode the minted role to
   `viewer` regardless of any `role` field the caller sends, or explicitly reject a
   `role` request above `viewer` from anyone but the admin token.
5. **`require_trade_scope()` itself is already correct** (`src/auth.py`, today's commit)
   — it checks scope on whatever token/session is present. Nothing here needs to change;
   the leak is entirely in what `/login` was willing to exchange for what.

## Verify when this comes back

- POST `michaeliscool` to `/login` on `vultr` → token returned should be
  `SUPERMCP_VIEWER_TOKEN`, **not** the value of `SUPERMCP_TOKEN`.
- Call `place_live_order` (or `dry_run_order`) using that returned token → must get
  `permission_denied`, not a preview/execution result.
- Michael's own connector (owner OAuth login or direct `SUPERMCP_TOKEN` bearer) still
  gets full `trade`/`admin` access, unchanged.
- `chadiusmaximus`'s existing minted key and any new self-minted key stay `viewer`/`read`
  only — spot-check with `python -m src.mcp_keys list`.

## Separately still true, not addressed by this doc

`UNGATED_ORDER_BROKERS=webull` still waives `LIVE_ORDERS_ENABLED` and the
`"SEND IT LIVE"` confirm phrase for Webull specifically (see
`docs/SUPERMCP_CONSOLIDATION_PLAN.md`, F1 / Phase 0). Locking scope to Michael's key
only narrows *who* can reach the order tool — it doesn't add back the caps, kill switch,
or confirm phrase on what happens once he (or anyone holding that key) does. Both should
land before this is called done for live trading.
