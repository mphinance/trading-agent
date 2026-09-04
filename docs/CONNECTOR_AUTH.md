# Connecting claude.ai to the trading-agent MCP server

Everything needed to reconnect the connector, and to understand what each
credential can and cannot do. Written 2026-09-04, after M8-24 armed the order
path.

**No secret values appear in this file, and none should ever be added to it.**
This repo is public and the work gets streamed (CLAUDE.md rule 5). Every entry
below tells you where a value lives, never what it is.

---

## 1. Where the credential lives

| | |
|---|---|
| Variable | `TRADING_AGENT_TOKEN` |
| File | `~/trading-agent/.env.trading-agent` **on the coolify box** |
| Host | `ssh coolify` |
| Format | 64 hex chars (`openssl rand -hex 32`) |

```bash
ssh coolify 'grep "^TRADING_AGENT_TOKEN=" ~/trading-agent/.env.trading-agent | cut -d= -f2-'
```

**Two decoys live beside it.** `~/trading-agent/.env` also defines
`TRADING_AGENT_TOKEN` and **no systemd unit reads it** — `load_dotenv()` finds
it, so it silently fills gaps, and editing it to rotate a credential changes
nothing the service reads. That confusion is what caused the 2026-09-03
incident. `.env.trading-agent.example` is the committed template; its values
are published on GitHub and must never become live ones. Confirm which file is
authoritative before editing anything:

```bash
ssh coolify 'systemctl --user cat trading-agent.service | grep EnvironmentFile'
```

---

## 2. The two credentials are not equivalent

This is the part worth remembering, because it is not obvious from the config.

| | Static bearer | OAuth-issued token |
|---|---|---|
| What it is | `TRADING_AGENT_TOKEN` itself | minted per-client at `/authorize` |
| Scopes | `read`, `safe-write` | `read`, `trade` |
| Can read the account | yes | yes |
| **Can place an order** | **no** | **yes** |
| Sees order tools in `tools/list` | no — filtered out | yes |
| Revocable individually | no | yes |
| Survives a server restart | yes (it's a file) | yes (`data/oauth_tokens_state.json`) |

A read-only bearer call to an order tool does not return `403`. FastMCP filters
tools by scope, so the tool is not in the listing at all and a direct call
answers `Unknown tool: 'submit_manual_proposal_tool'`. That looks like a
deployment failure and isn't — check the scope before you go debugging
registration.

The upshot: **the long-lived secret sitting in a file cannot move money.** Only
a token minted through the human-present gate can. Keep it that way — do not
"fix" this by adding `trade` to the bearer's scope list in `_build_auth()`.

---

## 3. Connecting the connector

1. claude.ai → **Settings → Connectors → Add custom connector**
2. URL: `https://agent.mphinance.com/mcp`
3. Press **Connect**. The browser opens `https://agent.mphinance.com/authorize`,
   which serves a plain HTML form naming the client and the scope it is asking
   for.
4. Paste `TRADING_AGENT_TOKEN` into that form as the operator key. It POSTs
   (never GETs — a GET would put the secret in Traefik's access log and the
   browser history on every reconnect).
5. Done. The connector holds a token scoped `read` + `trade`.

### If it 401s forever

**Symptom:** every request logs `invalid_token`, and no `/register` or
`/authorize` hit appears in the journal — the connector is replaying a dead
credential rather than re-authenticating.

**Cause:** registered clients live in an in-memory dict and are deliberately
dropped on restart (`oauth_provider.py.__init__`). The connector does not
re-run DCR on its own.

**Fix:** disconnect and re-add the connector on claude.ai. There is no
server-side action that repairs it.

```bash
# See what the gate is actually deciding
ssh coolify 'journalctl --user -u trading-agent.service -n 200 --no-pager \
  | grep -E "HTTP/1.1\" (200|401|403)" | tail -20'
```

---

## 4. What bounds an order

Five independent gates. All must pass; none is a substitute for another.

| Gate | Where | Current setting |
|---|---|---|
| Kill switch | `VESPER_TRADING` | **1** — in `.env.trading-agent` *and* `.env.vesper`, keep in step |
| Portfolio cap | `min(MCP_MAX_NOTIONAL, MCP_MAX_NOTIONAL_PCT × NLV)` | 25% of NLV, $1000 ceiling |
| Daily count | `MCP_MAX_DAILY_ORDERS` | 5/day, persisted in `data/mcp_daily_order_count.json` |
| Guard cap | `VESPER_MAX_NOTIONAL` | $2500 |
| Halt / breaker | `data/halt_state.json`, 15% trailing-peak NLV | clear |

The portfolio cap **fails closed**: if net liquidation value cannot be read,
the cap is `0` and every opening order is refused. It never falls back to the
flat ceiling — a cap computed from an unknown book is not a cap. Closing orders
skip it entirely, since they cannot increase exposure.

On a $1,513 book that makes the operative cap about **$378**, not $1000. The
flat ceiling is not the binding constraint at this account size and was never a
real limit: at $1000 it sat 2.5× above the account's actual buying power, so
the broker would have rejected anything it could have blocked.

**A tool can originate an order. No tool can approve a pending one.**
`resume()` and `ApprovalRegistry.submit_decision()` are unreachable from every
MCP module, pinned by an AST test. Buttons move money (CLAUDE.md rule 4d).

---

## 5. Health checks

```bash
# Service state and what it registered at boot
ssh coolify 'systemctl --user status trading-agent.service --no-pager | head -5
  journalctl --user -u trading-agent.service --since "10 min ago" --no-pager \
    | grep -E "Registered|tools registered"'

# Unauthenticated request must be refused
curl -s -o /dev/null -w "%{http_code}\n" https://agent.mphinance.com/mcp   # expect 401

# Scopes the server advertises
curl -s https://agent.mphinance.com/.well-known/oauth-authorization-server \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["scopes_supported"])'

# Kill switch agreement across both env contracts
ssh coolify 'grep -H "^VESPER_TRADING=" ~/trading-agent/.env.trading-agent \
                                        ~/trading-agent/.env.vesper'
```

If the unauthenticated probe ever returns `200`, stop the service immediately —
authentication is not gating.

---

## 6. Emergency stop

```bash
# Freeze everything (checked before anything else in the order path)
ssh coolify 'cd ~/trading-agent && ./.venv/bin/python vesper.py halt --reason "manual"'

# Or pull the kill switch and restart
ssh coolify 'sed -i "s/^VESPER_TRADING=.*/VESPER_TRADING=0/" ~/trading-agent/.env.trading-agent
             systemctl --user restart trading-agent.service'

# Or stop the surface entirely
ssh coolify 'systemctl --user stop trading-agent.service'
```
