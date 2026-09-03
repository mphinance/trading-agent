# Vesper & Trading-Agent Deployment Guide (Milestone 7)

## 1. Overview & Architecture

Vesper on Coolify (`agent.mphinance.com`) runs as two independent subsystems from one repository checkout:
1. **`trading-agent.service`**: Owner-only MCP server, bound to docker bridge `10.0.0.1:8500`, fronted by Traefik with TLS and OAuth 2.1 / static bearer auth.
2. **`vesper-loop.service` & `vesper-listen.service`**: The autonomous trading agent (scanner, playbooks, risk gates, Discord/Telegram bot approval, execution guard, and position monitor).

```
                      Internet (HTTPS)
                            │
                            ▼
              ┌───────────────────────────┐
              │      Traefik Reverse      │
              │     Proxy (Let's Encrypt) │
              └─────────────┬─────────────┘
                            │ http://host.docker.internal:8500
                            ▼
             ┌─────────────────────────────┐
             │    trading-agent.service    │
             │   (Bound: 10.0.0.1:8500)    │
             │  Reads: .env.trading-agent  │
             └──────────────┬──────────────┘
                            │ (Reads core/ data layer)
                            ▼
  ┌────────────────────────────────────────────────────────┐
  │         Shared State & Core Leaf Package               │
  │  data/halt_state.json, data/audit_chain.jsonl, etc.    │
  └─────────────────────────▲──────────────────────────────┘
                            │ (Reads core/ & moves money)
             ┌──────────────┴──────────────┐
             │    vesper-loop.service      │
             │   vesper-listen.service     │
             │     Reads: .env.vesper      │
             └─────────────────────────────┘
```

---

## 2. DNS & Hostname Routing (The Wildcard Footgun)

- **`agent.mphinance.com`** → Points specifically to the Coolify server (`5.161.247.12`).
- **`*.mphinance.com` Wildcard Footgun**:
  Notice that `*.mphinance.com` points by default to the Vultr server hosting SuperMCP.
  If you mistype the hostname (e.g. `trading.mphinance.com` or `mcp.agent.mphinance.com`), your traffic silently routes to Vultr instead of Coolify, resulting in an immediate, confusing `503 Service Unavailable` or certificate mismatch. Always ensure the exact host `agent.mphinance.com` is configured.

---

## 3. Traefik Dynamic Configuration (M7-04)

The Traefik configuration file is tracked in `deploy/traefik/trading-agent.yaml`:

```yaml
http:
  routers:
    trading-agent:
      rule: "Host(`agent.mphinance.com`)"
      entryPoints: [https]
      service: trading-agent
      tls:
        certResolver: letsencrypt
    trading-agent-http:
      rule: "Host(`agent.mphinance.com`)"
      entryPoints: [http]
      service: trading-agent
      middlewares: [trading-agent-tohttps]
  middlewares:
    trading-agent-tohttps:
      redirectScheme:
        scheme: https
        permanent: true
  services:
    trading-agent:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:8500"
```

### Manual Installation Step (Requires `sudo`)
To install or update the Traefik dynamic route on the host, a machine administrator must run:
```bash
sudo cp deploy/traefik/trading-agent.yaml /data/coolify/proxy/dynamic/trading-agent.yaml
```
Traefik detects file changes dynamically without restarting and automatically requests a Let's Encrypt certificate on the first inbound HTTPS request.

### Verifying Auth Gating
- An unauthenticated request must fail with HTTP 401 or 403:
  ```bash
  curl -s -o /dev/null -w "%{http_code}\n" https://agent.mphinance.com/mcp
  # Expected: 401
  ```
- If an unauthenticated request returns 200, authentication is NOT gating and the service must be halted immediately.
- Confirm no live credential still equals its `.example` placeholder. `deploy/install.sh` now refuses to deploy in that state, and the server refuses to open an http listener with a placeholder/low-entropy token (see `core/secret_hygiene.py`). Concrete check, prints nothing when healthy:
  ```bash
  diff <(grep -v '^#' ~/trading-agent/.env.trading-agent.example | cut -d= -f1) /dev/null >/dev/null; \
  for k in TRADING_AGENT_TOKEN WEBULL_APP_KEY WEBULL_APP_SECRET WEBULL_KEY WEBULL_SECRET TD_API_KEY TDPRO_API_KEY; do \
    v=$(grep "^${k}=" ~/trading-agent/.env.trading-agent | cut -d= -f2-); \
    ev=$(grep "^${k}=" ~/trading-agent/.env.trading-agent.example | cut -d= -f2-); \
    [ -n "$v" ] && [ "$v" = "$ev" ] && echo "STALE PLACEHOLDER: $k"; \
  done
  ```

---

## 4. Environment Contracts (M7-02)

Environment configuration is split into two distinct files with zero cross-contamination:
- **`~/trading-agent/.env.trading-agent`**: Sourced by `trading-agent.service` (`EnvironmentFile=%h/trading-agent/.env.trading-agent`). Contains `WEBULL_*`, `TD_API_KEY`, `TDPRO_API_KEY`, `SEC_USER_AGENT`, `TRADING_AGENT_TOKEN`, and `MCP_*`. Never contains `TELEGRAM_*` or `VESPER_TRADING`.
- **`~/trading-agent/.env.vesper`**: Sourced by `vesper-loop.service` and `vesper-listen.service`. Contains core credentials plus `TELEGRAM_*`, `DISCORD_*`, and `VESPER_TRADING=0`. Never contains `TRADING_AGENT_TOKEN` or `MCP_*`.

**Warning (2026-09-03):** a plain `~/trading-agent/.env` may also exist in the checkout. It is NOT read by any systemd unit — editing it has no effect on either service. Confusing it with `.env.trading-agent` caused the 2026-09-03 token incident (an operator following stale guidance pointed at `.env` while the live token sat, unrotated, in `.env.trading-agent`). Always confirm which file a unit actually reads with `systemctl --user cat trading-agent.service | grep EnvironmentFile` before editing credentials.

---

## 5. Deployment & Systemd Units (M7-03 & M7-08)

All units run as systemd **user** services (`systemctl --user`).

### Unit List
- `deploy/trading-agent.service` → `~/.config/systemd/user/trading-agent.service`
- `deploy/vesper-loop.service` → `~/.config/systemd/user/vesper-loop.service`
- `deploy/vesper-listen.service` → `~/.config/systemd/user/vesper-listen.service`

### Safe Startup Principle (M7-09)
Running `deploy/install.sh` builds the virtual environment, installs unit files, enables user lingering (`loginctl enable-linger`), and starts ONLY `trading-agent.service`.
`vesper-loop.service` and `vesper-listen.service` are enabled but kept explicitly **STOPPED** until live credentials and arming milestones are passed. This prevents crash-looping with `Restart=on-failure` on a shared server.

---

## 6. Independent Redeploy & Rollback Procedure (M7-06 & M7-07)

A deployment to one component must never disrupt or restart the other:
- **Updating MCP Server**:
  ```bash
  git pull
  systemctl --user restart trading-agent.service
  # Verify vesper units were untouched:
  systemctl --user show vesper-loop.service -p ActiveEnterTimestamp
  ```
- **Updating Vesper Engine**:
  ```bash
  git pull
  systemctl --user restart vesper-loop.service vesper-listen.service
  # Verify MCP unit was untouched:
  systemctl --user show trading-agent.service -p ActiveEnterTimestamp
  ```

### Rollback Runbook (M7-07)
If a bad commit causes failures, rollback without touching shared runtime state:
1. Revert to the last known good commit:
   ```bash
   git revert <bad_commit_sha> --no-edit
   # Or checkout specific commit: git checkout <good_sha>
   ```
2. Restart the affected unit:
   - For MCP: `systemctl --user restart trading-agent.service`
   - For Vesper: `systemctl --user restart vesper-loop.service vesper-listen.service`
3. State persistence guarantee:
   Runtime data files (`data/halt_state.json`, `data/audit_chain.jsonl`, `data/paper_ledger.json`, `data/checkpoints.sqlite`) are outside version control and remain fully intact across git checkouts and rollbacks.
