# Plan — the funnel, and how the three repos relate

**Revision 3, 2026-09-03.** Revision 2 planned a new public MCP repo as the
acquisition channel. That was written without knowing Vespryx existed in the
state it does. The funnel already exists, is already paid, and is already
distributed — so this revision inverts the strategy. Change log in §11.

---

## 1. Verdict

**The funnel is Vespryx, not a new repo.**

`tradernetwork/dealer-hud` (product name **Vespryx**, v0.22.12) is already:

- **shipping on the Chrome Web Store** —
  `https://chromewebstore.google.com/detail/vespryx/pogpghbkbbbjolibaeaefnfklhgchkob`
- **a paid product with its own subscription**, separate from TD Pro
  (`hasVespryx`; the code is explicit that "TD Pro subscription does not include
  Vespryx at any tier"), with entitlement probes and 403→paywall handling
- **trader-facing**, overlaying GEX / apex / unusual flow onto TradingView and
  cashtags across the web
- **already serving MCP tools** — `tools/td-mcp.mjs` registers **17**: five
  dealer-data (`get_gex_ticker`, `get_apex_levels`, `get_ticker`,
  `get_dealer_payload`, `get_token_status`) and twelve `tv_*` chart-control
  tools driving the user's own TradingView tab through `gui.mjs`
  (`127.0.0.1:7777`). Hand-rolled stdio JSON-RPC, no MCP SDK dependency.
- **already bridging browser session → headless tooling** —
  `tools/td-token.mjs` installs a `{access_token, refresh_token}` pair to a
  token file, and `tools/td-api.mjs` is the JWT client (Bearer header, refresh
  on 401)

Every problem revision 2 was going to spend months solving, Vespryx has already
solved: distribution, audience, a purchase moment, and access to **apex levels**
— which no paid API key can reach (§4).

Two things it has **not** solved, and they are the work: **conviction** is
exposed nowhere outside the web app (not even in `td-mcp.mjs`), and the
credential handoff is still manual copy-paste. The deliverable is therefore
**a device flow** (§5), plus one live bug to fix today (§9).

---

## 2. Three repos, linked but not merged

| repo | product | visibility | role |
|---|---|---|---|
| `mphinance/trading-agent` | **Vesper** — the cockpit: LangGraph agent, risk gate, order path, live account | public today | credibility and portfolio. Not a funnel. |
| `tradernetwork/dealer-hud` | **Vespryx** — Chrome extension + desktop tools + MCP server | private | **the funnel.** Paid, distributed, trader-facing. |
| `tradernetwork/quant-mcp` | thin open shell (§6) | would be public | a pointer, not a product. Optional. |

### Why they stay separate

- **Different languages and toolchains.** Vesper is Python; Vespryx is
  JS/Node + MV3.
- **Different release cadences.** Chrome Web Store review is days; PyPI is
  minutes; a private cockpit is push-when-ready. Merging couples the slowest to
  the fastest.
- **Different risk classes.** Vesper can move money. Vespryx is a consumer
  product under store review. Those should never share a release train.
- **Different audiences.** Traders install Vespryx. Developers read Vesper.

### How they link

Linking is cross-reference, not dependency:

- Vesper's README: "the dealer data this agent trades on comes from Vespryx →
  store link."
- Vespryx's docs: "the autonomous agent built on this data → Vesper."
- `quant-mcp` (if built): "want live dealer positioning on your chart? →
  Vespryx."

No repo imports another. No shared submodule. The link is a sentence and a URL.

---

## 3. Why the funnel inverted

Revision 2's plan had four problems. Vespryx answers all four; a new public repo
answers none of them.

| revision 2's problem | new repo | Vespryx |
|---|---|---|
| Discovery — the whole English-language US-equity MCP niche tops out at 40-120 stars (`options mcp` peaks at **39**) | ceiling of low hundreds | Chrome Web Store: real search, one-click install, install counts |
| Audience — MCP installers are developers; the buyer is a trader | targets the intersection | targets traders directly |
| No purchase moment; five context switches from intent to buy | had to be designed from scratch | subscription, entitlement checks and paywall handling already shipped |
| The free tier is a complete product, so nobody reaches the paywall | 47 free tools = the leak | the product *is* the paid thing |

---

## 4. What each auth path can actually reach

This is the fact that decides everything downstream.

| | **paid API key** (`td_live_`) | **session** (what Vespryx uses) |
|---|---|---|
| namespace | `/api/v1/*` + `/api/v1/mcp` | `/api/*` |
| gex / gamma, unusual activity | ✅ | ✅ |
| dark pool, directional flow, hedge, short data, smart money, premarket gappers | ✅ *(MCP mount only)* | ✅ |
| screeners, sectors, put/call, signals, earnings, institutional, insider, politician | ✅ | ✅ |
| **apex levels** | ❌ | ✅ |
| **conviction** | ❌ | ✅ |
| liquidity map, market health, IPO scanner, whale watch, fear/greed | ❌ | ✅ |
| watchlists, journal, follows, settings | ❌ | ✅ |

**The paid key surface is a strict subset of what a logged-in session sees**, and
the two biggest differentiators — apex levels and conviction — are on the wrong
side of that line for a key holder.

Their own licensing doc (`reference/data-licensing.md` §3) classifies both as
🟢 derived TDP IP and calls `get_apex_levels` "the showcase for a derived-only
license" — so exposing them to keys is *permitted*, just **not built**. No public
MCP tool module exists for either, on any branch.

Which leaves the choice:

- **Ride the session** (what Vespryx does) → apex available today, zero backend
  work. Browser-bound.
- **Build public tool modules** for apex and conviction → unblocks the key path.
  The pattern exists 18 times over in `src/mcp/public/tools/`, each with a
  projection allowlist and a test. Deferred, not rejected.

### The free/paid line already exists, and it is the right shape

Confirmed in `tools/td-api.mjs`:

- **`GET /gex/{sym}` and `GET /ticker/{sym}` need no Authorization header at
  all** — GEX is genuinely public.
- **`GET /gex/{sym}/apex` requires the session** and answers HTTP 200 with
  `{success:false, locked:true, error:'premium_required'}` when the account is
  not entitled.

That is *degradation, not a wall* — the exact gate shape revision 2 argued for,
already built: everyone gets gamma, apex is what you pay for. `get_dealer_payload`
even fetches both in parallel and degrades gracefully with an `apexNote` when
apex is locked.

**Conviction is not exposed by `td-mcp.mjs` at all** — no tool, no client call.
So of the two differentiators, apex is live over the session path and conviction
is not reachable anywhere outside the web app.

---

## 5. The device flow — the one thing worth building

### Today

`tools/td-token.mjs`: the user clicks a "copy token" button, pastes
`{access_token, refresh_token}` JSON into the CLI, and it lands in a token file.
`td-api.mjs` then holds the Bearer header and refreshes on 401.

Token file is `~/.cache/vespryx/td-token.json`, `{access, refresh}`, forced 0600.
`tdGet` refreshes once transparently on 401.

It works. It is also the friction point — and it has already failed expensively
in a way that argues for replacing it:

> `td-api.mjs:296-303` documents a real incident: **the session had been dead
> since 4 August and every tool answered "Dealer levels are a paid feature" on a
> paid-up plan.** Apex returns `200 {locked:true}` for an expired token as well
> as for a genuine entitlement failure, so a stale credential is indistinguishable
> from an unpaid one. The code now separates `SESSION_EXPIRED` from `LOCKED`, but
> the root cause is a credential that silently rots because a human has to
> re-paste it.

A manual token that expires quietly, on a product whose paywall message is the
failure mode, is worth engineering away.

### Proposed

The standard device-authorization flow, the same shape as `gh auth login`:

1. Headless tool prints a URL and a short code.
2. User opens it in the browser **where they are already signed in** — that's
   the existing session doing the work, no new auth surface.
3. Backend verifies the session and the **Vespryx entitlement** (`hasVespryx` —
   not TD Pro tier; the code is already careful about that distinction) and
   binds the code.
4. Tool polls, receives the credential, writes the token file.
5. Refresh continues exactly as today.

### What it needs

- **Backend:** an endpoint pair (`/api/device/code`, `/api/device/token`) in the
  Whop backend. Small, and it is the same mechanism a free-trial key would use.
- **Client:** device-code support in `td-token.mjs`, unchanged `td-api.mjs`.
- **Nothing in `trading-agent`.**

### Why it matters beyond convenience

It is also the **purchase moment**. A user who hits the flow without a Vespryx
entitlement gets, in the same response, a signup link with a referral parameter —
so the buy step happens inside the flow rather than sending them off to hunt a
pricing page. That is the single conversion surface revision 2 left undesigned.

---

## 6. What `quant-mcp` becomes

Substantially smaller, and genuinely optional.

Its job is **not** to be a 47-tool free product — that was the conversion leak.
A satisfying free screener/backtest/EDGAR toolkit means people install, get what
they came for, and never approach anything paid.

If it is built at all, it is a thin open shell whose README's most important line
is a link to Vespryx. Decide it **after** the device flow ships, with real
numbers from the store listing, not before.

`quant-mcp` is free on PyPI and npm; the name can be reserved cheaply and held.

---

## 7. Still true from revision 2 — do these regardless

1. **Land the security work.** 19 files uncommitted from the 2026-09-03
   placeholder-token incident: `core/secret_hygiene.py`, the startup guard, the
   fail-closed approver allowlist, `install.sh`'s placeholder refusal, the
   OAuth GET→POST fix, the A4 import pins, and the doc corrections. Suite is
   green at 765.
2. **Scrub production specifics**, case-insensitively. `mcp_server/` and `core/`
   are now clean; `docs/`, `deploy/` and `autonomous/` still carry ~200
   `coolify` and ~38 `agent.mphinance.com` references, and
   `mphinance/trading-agent` is public today.
3. **Verify TDPro tools actually work on the deployed box.** `mcp_server/`
   authenticates via `core/traderdaddy.py`, which needs
   `TRADERDADDY_API_URL`/`EMAIL`/`PASSWORD`; the live box has only `TD_API_KEY`
   and `TDPRO_API_KEY`. A large share of the 60 advertised tools may be dead.
4. **Rotate-and-reconnect hygiene.** `TRADING_AGENT_TOKEN` was rotated
   2026-09-03; the claude.ai connector needs re-adding, not editing, because the
   same secret signs OAuth tokens.

---

## 8. Sequence

| # | step | gate |
|---|---|---|
| 1 | Commit + push the security/doc work | `pytest -q` green (765) |
| 2 | Case-insensitive scrub, whole repo | `grep -i` clean |
| 3 | Verify the TDPro auth gap on the live box (§7.3) | tools return data |
| 4 | Confirm what `td-mcp.mjs` already serves | tool list known |
| 5 | Build the device flow: backend endpoint pair + `td-token.mjs` client | a stranger can auth without copy-paste |
| 6 | Put the signup/referral link in the un-entitled device-flow response | purchase moment exists |
| 7 | Instrument: store installs, device-flow completions, attributed signups | a number, written down |
| 8 | Add cross-links between the three repos | — |
| 9 | *Optional, later:* public apex/conviction tool modules | licensing projection + guard test |
| 10 | *Optional, later:* decide whether `quant-mcp` ships at all | store numbers say yes or no |

---

## 9. Bug found while auditing — fix this one today

**`QUICKSTART.md:27-60` tells users to clone a private repo.**

"Path 2: Desktop CDP Automation & Coding Agents" is published, user-facing
instruction aimed at Claude Code / Cursor users on a `beginner+` Vespryx tier.
It says to run:

```
git clone https://github.com/tradernetwork/dealer-hud
```

`tradernetwork/dealer-hud` is **private**. Every non-collaborator who follows
that path gets a 404 — on a documented, paid-tier feature. Either the repo
(or a `tools/`-only subset) needs to be published, or Path 2 needs a different
distribution mechanism, or the doc needs to stop promising it.

This also answers what `td-mcp.mjs`'s distribution actually is:
`tools/package.mjs:348-353` explicitly excludes `tools/`, `test/`, `site/`,
`docs/` and `pine/` from the Chrome Web Store zip, with a hard leak check that
exits non-zero if any slips in. So the extension never ships the MCP server —
the clone path is the only route to it, and it is currently broken for
everyone outside the org.

---

## 10. Open questions

1. ~~Does `td-mcp.mjs` ship to users?~~ **Answered:** not in the extension
   package; only via the (currently broken) clone path in QUICKSTART Path 2.
2. ~~Does it expose apex and conviction?~~ **Answered:** apex yes
   (`get_apex_levels`, plus inside `get_dealer_payload`); conviction no, nowhere.
3. **Does anything in Vespryx go public?** It is the product being sold, so
   opening it is a different decision from opening Vesper — but §9 forces the
   question, because Path 2 already assumes it is public.
4. **Free-trial shape.** The Whop backend has no no-card path today —
   `has_api_access` flips only on a completed Stripe subscription, and the 5-day
   trial requires a card. A device flow that can only ever return "you must pay
   first" is a worse funnel than one that can hand out a limited trial.

---

## 11. Change log

| revision 2 said | revision 3 |
|---|---|
| Build a public MCP repo as the funnel | The funnel already exists and is on the Chrome Web Store |
| Design a purchase moment from scratch | Vespryx already has a subscription, entitlement checks and paywall handling |
| MCP installers are the audience | Traders are, and the extension already reaches them |
| Session auth "does not transfer" to headless | It already does — `td-token.mjs` + `td-api.mjs`, manually |
| Apex/conviction need new backend work | Reachable via session today; backend modules are an optimisation, not a blocker |
| `quant-mcp` is the deliverable | The device flow is the deliverable; `quant-mcp` is optional and later |

**Unchanged and still verified:** the 16-module public manifest and its zero
`vesper`/`trading_mcp` reachability; no broker client in that graph; the
placeholder-token incident and its four guards; `quant-mcp` free on PyPI and npm.
