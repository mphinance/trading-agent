# Coolify Host Infrastructure Map

Read-only snapshot of the remote host reachable as `ssh coolify` (public IP
`5.161.247.12`, Hetzner, Ubuntu 24.04), taken 2026-09-02. No service was
started, stopped, restarted, deployed, or modified to produce this document.
Where something could not be determined, that is stated explicitly rather
than guessed.

Hostname: `ubuntu-4gb-ash-1-traderdaddy`.

---

## 1. Host

| Item | Value |
|---|---|
| CPU | 3 vCPU, AMD EPYC-Rome (KVM guest) |
| RAM | 3.7 GiB total, 1.6 GiB used, 1.1 GiB free, 2.1 GiB "available" (incl. reclaimable cache) |
| Swap | 8.0 GiB total, **2.2 GiB in use** |
| Disk (`/`, `/dev/sda1`) | 75 G total, 55 G used, 18 G free (**77% used**) |
| Uptime | 8 days, 6h+ (as of snapshot); load average 0.34 / 0.36 / 1.08 |
| Kernel | `6.8.0-138-generic`, Ubuntu 24.04.3 LTS (noble) |
| Docker | 27.0.3 (Community), Compose v5.1.0, Buildx v0.31.1; 14 containers total (12 running, 2 exited), 15 images |
| SSH user | `mph` (uid 1000), groups `mph, sudo, docker` |
| Sudo | **Passwordless** — `sudo -n true` succeeds with no prompt |

Swap is meaningfully in use on a 3.7 GiB box; see "Things that would bite."

---

## 2. Containers

### Running (12)

| Name | Image | Status | Restart policy | Published ports | Networks | Mem usage |
|---|---|---|---|---|---|---|
| `coolify` | ghcr.io/coollabsio/coolify:4.0.0-beta.468 | Up 8d (healthy) | always | 8000→8080 on **0.0.0.0** | coolify | 161 MiB |
| `coolify-db` | postgres:15-alpine | Up 8d (healthy) | always | none published | coolify | 22 MiB |
| `coolify-redis` | redis:7-alpine | Up 8d (healthy) | always | none published | coolify | 6.8 MiB |
| `coolify-realtime` | ghcr.io/coollabsio/coolify-realtime:1.0.11 | Up 8d (healthy) | always | 6001-6002 on **0.0.0.0** | coolify | 16.5 MiB |
| `coolify-proxy` | traefik:v3.6 | Up 8d (healthy) | unless-stopped | 80, 443/tcp, 443/udp, 8080 on **0.0.0.0** | coolify, e10bttp3ewl6jinj08606h5q | 40 MiB |
| `coolify-sentinel` | ghcr.io/coollabsio/sentinel:0.0.22 | Up 8d (healthy) | **no** | none published | bridge (default) | 12.7 MiB |
| `bot-e10...` (TraderDiscord-v2 "bot") | `e10bttp3ewl6jinj08606h5q_bot` | Up 4d (healthy) | unless-stopped | 8300 on **0.0.0.0** (see §6 — blocked from the internet at the firewall layer) | e10bttp3ewl6jinj08606h5q | 53.5 MiB |
| `ingest-e10...` (TraderDiscord-v2 "ingest") | `e10bttp3ewl6jinj08606h5q_ingest` | Up 4d (healthy) | unless-stopped | 8200 on **0.0.0.0** (blocked, see §6) | e10bttp3ewl6jinj08606h5q | 31.5 MiB |
| `yt-notify-e10...` | `e10bttp3ewl6jinj08606h5q_yt-notify` | Up 4d | unless-stopped | none published | e10bttp3ewl6jinj08606h5q | 33.3 MiB |
| `whitelabel-bot` | whitelabel-bot:latest | Up 8d | unless-stopped | none published | bridge (default) | 26.6 MiB |
| `deepdive` | deepdive:latest | Up 8d (healthy) | unless-stopped | 8000/tcp exposed, **not published** to host | coolify | 20.6 MiB |
| `sam-dashboard` | sam-dashboard:latest | Up 8d | unless-stopped | 8400 on **0.0.0.0** — **not** covered by any firewall rule, see §6 | bridge (default) | 2.6 MiB |

### Exited / stopped (2)

| Name | Image | Exit code | When | Restart policy | Notes |
|---|---|---|---|---|---|
| `x-growth-agent` | x-growth-agent:latest | 137 (SIGKILL) | 22h before snapshot | unless-stopped | `OOMKilled=false` per `docker inspect` — this was a manual `docker stop`/`kill`, not an OOM event. `unless-stopped` means Docker will **not** bring it back on its own after an explicit stop. Log tail shows it was actively posting/replying up to the moment it was killed. |
| `csptracker-postgres` | postgres:15-alpine | 0 (clean) | ~7 weeks before snapshot | unless-stopped | Stopped cleanly and never restarted; sitting dormant for ~2 months. |

### Volumes / mounts (running containers)

- `coolify` — bind mounts under `/data/coolify/{databases,services,ssh,applications,backups}` plus `/data/coolify/source/.env` (ro).
- `coolify-proxy` — `/data/coolify/proxy` (rw), `/var/run/docker.sock` (ro).
- `coolify-sentinel` — `/var/run/docker.sock` (rw, needed to read container stats), `/data/coolify/sentinel` (rw).
- `coolify-db` — named volume `coolify-db`.
- `coolify-redis` — named volume `coolify-redis`.
- `bot` / `ingest` / `yt-notify` — each has its own named volume (`..._bot-data`, `..._ingest-data`, `..._yt-notify-data`) under `/var/lib/docker/volumes/`.
- `deepdive` — binds `~/deep_dive/cache` (rw), `~/ibkr/mur/docs/briefs` (ro), `~/ibkr/mur/data/recaps` (ro).
- `sam-dashboard` — binds `/opt/sam-dashboard/data` (ro).
- `whitelabel-bot` — no mounts.

---

## 3. Coolify itself

- **Version:** `ghcr.io/coollabsio/coolify:4.0.0-beta.468` (from the image tag; no separate version file found inside the container).
- **Coolify's own infrastructure** (6 containers): `coolify`, `coolify-db`, `coolify-redis`, `coolify-realtime`, `coolify-proxy`, `coolify-sentinel`.
- **Deployed via Coolify's application system:** only **one** application is registered in Coolify's own Postgres (`select * from applications`) — `mphinance/-trader-discord-v2` (branch `feat/coolify-deploy-e10bttp3ewl6jinj08606h5q`), which is the `bot` / `ingest` / `yt-notify` compose group (project id `e10bttp3ewl6jinj08606h5q`). Its definition lives at `/data/coolify/applications/e10bttp3ewl6jinj08606h5q/` (`docker-compose.yaml`, `.env`, `README.md`).
- **Everything else running is outside Coolify's management** — `whitelabel-bot`, `deepdive`, `sam-dashboard`, `x-growth-agent`, `csptracker-postgres` were started by plain `docker run`/compose invocations, not through the Coolify UI/API. `/data/coolify/services/` (Coolify's one-click-service store) is empty.
- **Scheduled tasks:** queried `scheduled_tasks` in the Coolify Postgres database directly — **0 rows**. No Coolify-native scheduled tasks are configured.

---

## 4. Traefik / ingress

`coolify-proxy` (Traefik v3.6) reads dynamic config from `/data/coolify/proxy/dynamic/*.yml|yaml`. Contents (router rules only, no secret values):

| File | Hostname | Routes to | TLS |
|---|---|---|---|
| `default_redirect_503.yaml` | catchall `/` | `noop` (empty backend, 503) | letsencrypt, priority -1000 |
| `trading-agent.yaml` | `agent.mphinance.com` | `http://host.docker.internal:8500` | letsencrypt |
| `discord.yml` | `discord.mphinance.com` | `/webhook/intake/*` → `ingest:8200`; everything else → `bot:8300` (container-to-container by compose alias, same `e10bttp3ewl6jinj08606h5q` network) | letsencrypt |
| `positions.yml` | `positions.mphinance.com` | `http://host.docker.internal:8771` | letsencrypt |

Static Traefik config also present but not itemized: `Caddyfile` (2 lines, artifact of an earlier setup, not the active proxy).

**Certificates issued** (from `acme.json`, domain names only): `positions.mphinance.com`, `deepdive.mphinance.com`, `discord.mphinance.com`, `agent.mphinance.com`. All four have a live letsencrypt certificate on file.

**`agent.mphinance.com` → trading-agent, confirmed state:**
- Traefik router is configured and has a valid TLS cert.
- Backend target is `host.docker.internal:8500`, which Traefik resolves to the host via its host-gateway mapping.
- On the host, `ss -ltnp` confirms something is actually listening: `10.0.0.1:8500`, owned by `python`, pid matching the `trading-agent.service` **systemd user unit**, which is `active (running)` (see §5) — this is `python -m trading_mcp.server` from `/home/mph/trading-agent/.venv`.
- The bind address is `10.0.0.1` (the `coolify` Docker bridge's gateway IP), **not** `0.0.0.0` and **not** loopback — matching the comment left in the config file itself ("Traefik is containerised and cannot reach the host loopback"). It is not directly reachable from the public interface; only containers that can route to that bridge gateway (which Traefik can, via `host.docker.internal`) can reach it.
- End-to-end: `agent.mphinance.com` is live, TLS-terminated at Traefik, and proxied to a running backend. The dynamic-config comment states the bearer token (`TRADING_AGENT_TOKEN`) is the actual access gate at the application layer; this snapshot did not (and per the read-only/no-secrets constraint, should not) test that gate.

  **Update, 2026-09-03:** the gate has since been verified directly — an
  unauthenticated request returns 401, and a request carrying the correct
  token returns 200. Separately, OAuth 2.1 (dynamic client registration) is
  now mounted alongside the static bearer, since `MCP_PUBLIC_URL` is set.

---

## 5. systemd units

### System units (non-stock, currently loaded/running)

| Unit | State | Notes |
|---|---|---|
| `fail2ban.service` | active/running | See §6 firewall — actively banning IPs (17 bans present in `ufw status`). |
| `atd.service` | active/running | Deferred-execution daemon (stock but listed since it's active). |
| `qemu-guest-agent.service` | active/running | Hetzner/KVM guest agent. |

No custom system-level (root) services beyond the standard Ubuntu server set were found; the interesting, project-specific units all live at the **user** level.

### System timers

Standard Ubuntu maintenance timers only (`apt-daily`, `apt-daily-upgrade`, `dpkg-db-backup`, `logrotate`, `man-db`, `fstrim`, `e2scrub_all`, `sysstat-collect`/`summary`, `motd-news`, `update-notifier-*`, `systemd-tmpfiles-clean`) — nothing custom at the system level.

### User (`mph`) units — non-stock

| Unit | State | ExecStart | Restart | Notes |
|---|---|---|---|---|
| `trading-agent.service` | **active/running**, enabled | `/home/mph/trading-agent/.venv/bin/python -m trading_mcp.server` | on-failure | Backing service for `agent.mphinance.com`, see §4. Running since 2026-09-01 17:34. |
| `mmr-positions.service` | active/running, enabled | `/home/mph/ibkr/mmr/.venv/bin/python -u scripts/positions_server.py` | always | Backs `positions.mphinance.com`. Binds `0.0.0.0:8771` (see §6). |
| `disclaw.service` | active/running, enabled | `bun run src/bot.ts` (in a `disclaw` dir under `~/nyx`) | on-failure | Discord bot ("nyx"). |
| `nyx-bridge.service` | active/running, enabled | `python3 /home/mph/TraderDiscord-v2/nyx_bridge/server.py` | always | Binds `10.0.2.1:8770` only (docker-bridge-scoped). |
| `ibkr-bridge.service` | **inactive/dead**, **disabled** | `/home/mph/ibkr/ibkr-gateway/venv/bin/python bridge.py` | always | Not currently running, not enabled to start. |
| `feedbackbot-health-watch.service` | **failed**, static (timer-triggered) | `/home/mph/bin/feedbackbot-health-watch` | no | See below — fails every run. |
| `investorgirl-verified-watch.service` | **failed**, static (timer-triggered) | `/home/mph/bin/investorgirl-verified-watch` | no | See below — fails every run. |
| `warroom-sync.service` | inactive/dead (last run exit 0), static | `/home/mph/warroom/scripts/sync.sh` | no | Runs on a timer, last run succeeded. |
| `cusage-discord.service` | inactive/dead (last run exit 0), static | `/home/mph/bin/cusage-discord` | no | Runs on a timer, last run succeeded. |

**Two crash-looping timer-triggered services**, confirmed via `journalctl --user`:

- `feedbackbot-health-watch.service` — every run for at least the last 3 runs (roughly hourly) exits 1 with `line 90: cd: /home/mph/main/backend: No such file or directory` (plus "`railway logs` returned nothing — could not check for the 429 pattern this run"). The target directory does not exist on this host.
- `investorgirl-verified-watch.service` — every run for at least the last 3 runs (every 15 min) exits 1 with `unexpected member payload: {"message": "Unknown Member", "code": 10007}` (Discord "unknown member" — the user it's tracking is no longer resolvable in the guild).

### User timers

| Timer | Schedule | Last run | Next run |
|---|---|---|---|
| `cusage-discord.timer` | ~every 5 min | 15:22:27 | 15:27:27 |
| `warroom-sync.timer` | ~every 10 min | 15:18:37 | 15:28:37 |
| `investorgirl-verified-watch.timer` | ~every 15 min | 15:17:02 | 15:32:00 |
| `feedbackbot-health-watch.timer` | ~hourly | 15:17:02 | 16:17:00 |
| `launchpadlib-cache-clean.timer` | ~daily | 09:16:37 | next day 09:16:37 |

Linger is enabled for `mph` (`loginctl show-user mph -p Linger` → `Linger=yes`), so these user units run independent of any logged-in session and survive a reboot without a login.

---

## 6. What is listening

Full `ss -ltnp` (owning process included), grouped:

**Bound to `0.0.0.0` / `[::]` (internet-facing unless the firewall blocks it):**

| Port | Process | Firewall disposition |
|---|---|---|
| 22 | sshd | Allowed (SSH), protected by fail2ban |
| 80, 443/tcp, 443/udp | docker-proxy → coolify-proxy | Allowed (HTTP/S, HTTP3) |
| 8000 | docker-proxy → coolify (dashboard) | Allowed explicitly in ufw |
| 6001, 6002 | docker-proxy → coolify-realtime | Allowed explicitly in ufw |
| 8080 | docker-proxy → coolify-proxy (Traefik dashboard/API) | **Not in the ufw allow list** — Traefik is host-published on `0.0.0.0:8080`, and because this is a Docker-published port it's handled by the kernel FORWARD chain, not ufw's INPUT chain (see below) — **effectively reachable from the internet**, gated only by whatever auth Traefik's own dashboard has, which was not checked (no secrets probed). |
| 8200 | docker-proxy → `ingest` container | Docker-published on `0.0.0.0`, but explicitly **DROP**ped for all sources in the `DOCKER-USER` iptables chain (`multiport dports 5900,6080,4003,4004,8200,8300`). Not internet-reachable. |
| 8300 | docker-proxy → `bot` container | Same `DOCKER-USER` DROP rule as 8200. Not internet-reachable. |
| 8400 | docker-proxy → `sam-dashboard` container | **Not** in the `DOCKER-USER` drop list and **not** in the ufw allow list. Docker-published ports are DNAT'd in `nat`/`DOCKER` and then hit unconditional `ACCEPT` rules in the `filter`/`FORWARD` chain before ufw's own forward chain is even consulted — **this port is internet-reachable today**, protected by nothing but the application itself (which is read-only-mounted data). See "Things that would bite." |
| 8771 | python (`mmr-positions.service`) | Process binds `0.0.0.0:8771`, but this is a **host** process (not Docker-published), so it terminates via the INPUT chain, which ufw does filter. The one ufw rule for this port only allows source `10.0.1.0/24` to destination `10.0.0.1:8771`; a request to the **public** IP on 8771 has no matching allow rule and is dropped by ufw's default-deny. Net effect: not publicly reachable despite the wildcard bind, but this relies on ufw's destination-IP matching rather than the process binding narrowly — a fragile pattern (see below). |

**Bound to a Docker bridge / internal IP only (not 0.0.0.0):**

| Port | Process | Address |
|---|---|---|
| 8500 | python (`trading-agent.service`) | `10.0.0.1` (coolify bridge gateway) — reachable only from containers routed to that bridge, e.g. `coolify-proxy`. |
| 8770 | python3 (`nyx-bridge.service`) | `10.0.2.1` (that project's bridge gateway) — ufw further scopes it to `10.0.2.0/24`. |

**Bound to loopback only:**

| Port | Process |
|---|---|
| 53 | systemd-resolved (`127.0.0.54`, `127.0.0.53`) |
| 8766 | sshd (a second sshd instance, `127.0.0.1` / `::1` only — likely a `ssh -L`/reverse-tunnel listener, not further identified) |

**Firewall reality check:**
- **ufw is active**, default-deny incoming, default-deny routed. It correctly filters host-terminated (INPUT-chain) traffic: SSH, the Coolify dashboard, HTTP/S, realtime websockets, and the two host-process services (8770, 8771) are all deliberately scoped.
- **ufw does NOT filter Docker-published ports** on this host — Docker inserts its own `DOCKER-USER`/`DOCKER`/`FORWARD` rules ahead of ufw's forward chain, and only a hand-maintained blocklist (`DOCKER-USER`: 5900, 6080, 4003, 4004, 8200, 8300) closes that gap. Any container port published without also being added to that blocklist (or otherwise firewalled) is exposed to the whole internet regardless of what `ufw status` shows. `sam-dashboard` on port 8400 is the concrete instance of this found in this snapshot; Traefik's own port 8080 dashboard is a second.
- `fail2ban` is active and has 17 IPs currently banned (sshd brute-force + "recidive").

---

## 7. ChromaDB

**Not a container** — no `chroma`/`chromadb` image or container exists on this host (`docker ps -a` has no match). It is a **Python package**, installed once at the user level:

- `~/.local/lib/python3.12/site-packages/chromadb` — version **1.5.9**, installed via `pip install --user` (not inside any project's `.venv`). `~/.local/bin/chroma` is the matching CLI entry point.
- **`~/trading-agent/.venv` does NOT have chromadb installed** (`pip show chromadb` → not found). This matches this repo's own CLAUDE.md: `mcp_server/knowledge.py` imports chromadb at module level and every caller catches the ImportError and degrades to `{"available": False, ...}` — on this host, any Vesper trade-memory / knowledge-base feature that depends on chromadb is currently in that degraded state, since the package isn't present in the venv the `trading-agent.service` unit actually runs from.

**Who does use it (persistent-client, embedded mode, via the shared `~/.local` install):**

| Project | Data path | Size |
|---|---|---|
| TraderDaddy-Pro---Whop (backend) | `~/TraderDaddy-Pro---Whop/backend/data/chromadb` (+ a second `chroma-tradier` collection dir) | 14 MB + 3.7 MB |
| `~/Michael/algo/ibkr/mur` | `~/Michael/algo/ibkr/mur/data/chroma` | 52 MB (venv itself not found separately — mmr, the deployed sibling, has no chroma dir) |
| `~/alpha-skills/skills/urithiru` | `~/alpha-skills/skills/urithiru/chroma_db` | 188 KB |
| TraderDiscord-v2 (`bot/chromadb_writer.py`, `seed_chromadb.py`) | data path not separately inspected; project directory total is 2.3 GB (mostly `.git`, 2.0 GB) | not isolated |

Numerous other chroma data directories exist under `~/td-*` staging/cutover repos and `.claude/worktrees/` copies of TraderDaddy-Pro---Whop — these are working-copy duplicates of the same backend, not independent deployments, each in the low tens of MB.

No process currently has a chroma server socket open (checked for the chroma-server default port 8000 — that port belongs to `coolify`, not chroma). All chroma usage found is the embedded/`PersistentClient` mode reading/writing SQLite-backed collections directly, not a client/server chroma instance.

---

## 8. Python environments

Every `.venv`/virtualenv found under `/home` and `/opt` (via `pyvenv.cfg`), excluding `uv`'s internal package cache:

| Path | Python | Size | Project |
|---|---|---|---|
| `~/trading-agent/.venv` | 3.12.3 | 1.1 GB | This repo, backs `trading-agent.service` |
| `~/ibkr/mmr/.venv` | 3.12.3 | 926 MB | Backs `mmr-positions.service` (positions.mphinance.com) |
| `~/mphinance/.venv` | 3.12.3 | 904 MB | `mphinance` project |
| `~/x-growth-mph/.venv` | 3.12.3 | 389 MB | `x-growth-mph` |
| `~/x-growth-agent/.venv` | 3.12.3 | 389 MB | `x-growth-agent` (source for the exited container, or a host-side copy — not disambiguated) |
| `~/hermes-agent/.venv` | 3.11.15 | 171 MB | `hermes-agent` |
| `~/mphinance-backup/tmp/pw_venv` | 3.11.2 | 165 MB | Playwright venv inside a backup tree |
| `~/ibkr/.venv-pw` | 3.12.3 | 156 MB | Playwright venv for `ibkr` |
| `~/apex-fieldtest-tracker/.venv` | 3.12.3 | 57 MB | `apex-fieldtest-tracker` |
| `~/acc-playbook/.venv` | 3.12.3 | 37 MB | `acc-playbook` |

Nothing was found under `/opt` — `/opt` contains only `x-growth-agent`, `x-growth-agent.old`, `sam-dashboard` and `containerd`, none with their own venv (the `x-growth-agent` container is presumably built from `~/x-growth-agent`, not `/opt/x-growth-agent`, which appears to be a deploy/build artifact directory instead).

---

## 9. Trading-related checkouts

### `~/trading-agent` (this repo, deployed copy)

- **Branch:** `main`
- **HEAD:** `c2e200458c146bc338ee86d0bd93d15f72366e7f` — "feat(trading_mcp): owner-only read-only MCP server over Vesper state" (2026-09-01 15:58:29 -0500)
- **Working tree:** clean (`git status --short` — 0 lines)
- **`.env` variable names present:** `TRADING_AGENT_TOKEN` only. No broker credentials (`WEBULL_*`, `TD_API_KEY`, etc.) are present in this checkout's `.env` — consistent with this deployment being the read-only `trading_mcp` surface only, not the full order-placing agent.

  **SUPERSEDED as of 2026-09-03.** This claim was about `~/trading-agent/.env`, which
  is NOT the file the service reads. The box now holds live Webull production
  credentials and TDPro keys in `~/trading-agent/.env.trading-agent`, a
  separate, gitignored file that this snapshot did not inspect. That exact
  distinction, between `.env` and `.env.trading-agent`, is what caused the
  2026-09-03 token incident: the production `TRADING_AGENT_TOKEN` was left at
  its committed example placeholder value. Rotated; see `deploy/README.md` and
  `docs/NEXT_STEPS.md` for the guards added since.

### Other trading-adjacent checkouts found on the host (not deeply audited — scope was "note")

| Path | What it appears to be |
|---|---|
| `~/Michael/algo/ibkr/mur` | git repo, branch `main`, HEAD `d941293d…` ("feat: Add postmortem feedback loop and fix strategy reviewer model", 2026-05-07), **15 modified/untracked paths** (dirty). Source for the Momentum Phund positions dashboard. |
| `~/ibkr/mmr` | The deployed sibling of `mur` backing `mmr-positions.service` / positions.mphinance.com — **not a git repository** at this path (`fatal: not a git repository`), so it's a plain deployed copy, not a checkout. |
| `~/ibkr/ibkr-gateway` | Present; backs the disabled/inactive `ibkr-bridge.service`. |
| `~/TraderDaddy-Pro---Whop` | 5.7 GB, includes many `.claude/worktrees/*` copies (branch-per-worktree dev pattern) — the TraderDaddy Pro signals backend that `td.py`/TDPro integration in this repo talks to. |
| `~/TraderDiscord-v2` | 2.3 GB, source for the Coolify-managed `bot`/`ingest`/`yt-notify` containers (Discord side of TraderDaddy). |
| `~/alpha-command-center` | Present on disk; not further inspected (out of scope per this repo's own CLAUDE.md, which describes it as someone else's production system this repo doesn't own). |

No other unexpected trading/broker checkouts turned up in a home-directory-wide scan.

---

## 10. Disk hogs

### Top by size under `/home` (depth 2, `sudo du`)

| Size | Path |
|---|---|
| 34 G | `/home/mph` (total) |
| 5.7 G | `~/TraderDaddy-Pro---Whop` |
| 5.1 G | `~/Michael` |
| 4.4 G | `~/.local` |
| 3.4 G | `~/Michael/repos` |
| 3.1 G | `~/.local/share` |
| 3.0 G | `~/TraderDaddy-Pro---Whop/.claude` (worktrees) |
| 3.0 G | `~/.claude` |
| 2.3 G | `~/TraderDiscord-v2` |
| 2.3 G | `~/td-tm-cutover` |
| 2.1 G | `~/mphinance` |
| 2.0 G | `~/TraderDiscord-v2/.git` |
| 1.9 G | `~/TraderDaddy-Pro---Whop/frontend` |
| 1.9 G | `~/td-tm-cutover/frontend` |
| 1.9 G | `~/.claude/projects` |
| 1.3 G | `~/Michael/algo` |

### Top by size under `/opt`

`/opt` totals only **2.7 MB** — `x-growth-agent` (1.7 MB), `x-growth-agent.old` (904 KB), `sam-dashboard` (156 KB). Not a factor in disk usage.

### Top by size under `/var/lib/docker`

| Size | Path |
|---|---|
| 11 G | `/var/lib/docker` (total) |
| 11 G | `/var/lib/docker/overlay2` (container/image layers) |
| 728 M | `/var/lib/docker/volumes` |
| 1.0 G | single overlay2 layer `92332c81...` |
| 1.0 G | single overlay2 layer `70147129...` |
| 916 M | single overlay2 layer `0c5ad1c5...` |
| 877 M ×2 | two more large overlay2 layers |
| 484 M | `e10bttp3ewl6jinj08606h5q_bot-data` volume |

`.git` directories and `.claude/worktrees/` copies account for a large share of `/home` usage (`TraderDiscord-v2/.git` alone is 2.0 GB of its 2.3 GB total; TraderDaddy-Pro---Whop carries multiple full worktree copies at ~3 GB).

---

## Things that would bite

Concrete, observed risks — not speculation:

1. **`sam-dashboard` (port 8400) is internet-reachable and not covered by any firewall rule.** It's Docker-published on `0.0.0.0:8400`; `ufw status` shows no allow rule for it, and it is absent from the `DOCKER-USER` iptables blocklist that protects 8200/8300. Because Docker-published ports are handled in the kernel `FORWARD` chain ahead of ufw's own forward rules, this port bypasses ufw entirely. It is currently reachable from the public internet with only the application's own (unaudited) behavior as protection.

2. **Traefik's own dashboard/API on port 8080 is Docker-published to `0.0.0.0` and is also not in `ufw status`'s allow list or the `DOCKER-USER` blocklist** — same class of exposure as #1, for the reverse-proxy's own admin surface. Its actual auth posture was not checked.

3. **ufw does not protect Docker-published ports as a class**, and the current protection for 8200/8300 depends entirely on a hand-maintained `DOCKER-USER` port list staying in sync with whatever gets published next. Any newly deployed container that publishes a port and isn't added to that list is exposed by default — this is exactly what happened with 8400.

4. **Two systemd user services are crash-looping on their timers**: `feedbackbot-health-watch.service` (hourly, fails every run — `cd: /home/mph/main/backend: No such file or directory`, a path that doesn't exist on this host) and `investorgirl-verified-watch.service` (every 15 min, fails every run — Discord "Unknown Member", the tracked user is no longer resolvable). Both have been failing for at least several consecutive runs at snapshot time.

5. **`x-growth-agent` container is stopped (exit 137, manual kill, not OOM) and won't restart on its own** — its `unless-stopped` policy means Docker will not bring it back after an explicit stop. It was actively working (posting/replying) up to 22 hours before this snapshot, so if this wasn't intentional, it has been silently down since.

6. **`csptracker-postgres` has sat exited for ~7 weeks** with an `unless-stopped` policy that likewise won't auto-restart it — either genuinely retired or a forgotten dependency for something else.

7. **`coolify-sentinel` has an explicit `no` restart policy** — unlike every other Coolify-owned container (which are `always`), a crash of the monitoring/metrics container will not self-heal.

8. **Root disk is at 77% used (18 GB free of 75 GB)**, and `/home/mph` alone is 34 GB with heavy duplication: multiple full `.claude/worktrees/` copies of `TraderDaddy-Pro---Whop`, plus near-duplicate `td-tm-cutover`/`td-affiliate-land`/other `td-*` staging trees, each carrying their own multi-hundred-MB `frontend` and chromadb data. Python venvs alone total roughly 4+ GB across ten projects (`trading-agent` and `mmr` are the largest at ~1 GB / 926 MB). This is not urgent but is a shrinking runway if left unmanaged.

9. **`mmr-positions.service` binds `0.0.0.0:8771`** (wildcard) rather than the docker-bridge-scoped address the sibling `nyx-bridge.service` uses for 8770. It is currently *not* publicly reachable only because the one ufw rule for it matches destination `10.0.0.1`, not the public IP — correct today, but a narrower bind (matching the `10.0.2.1`-style pattern used elsewhere on this host) would remove the dependency on that ufw rule staying correct.

10. **Swap is in active use (2.2 GiB of 8 GiB)** on a host with only 3.7 GiB of RAM running 12 containers plus ~9 always-on systemd user services — there is limited headroom, and the earlier `x-growth-agent` kill (even though this snapshot found it was a manual stop, not OOM) shows memory pressure is a live concern worth watching.

11. **`~/ibkr/mmr` (the code actually running as `mmr-positions.service`) is not a git repository** — there is no version control on the deployed positions-dashboard code at that path; its sibling `~/Michael/algo/ibkr/mur` is a git repo but is 15 files dirty, so it's unclear which state is actually deployed versus what's committed.

12. **Deployment provenance is split**: only one app (`TraderDiscord-v2`) is managed through Coolify's own application system; everything else running (`whitelabel-bot`, `deepdive`, `sam-dashboard`, `x-growth-agent`, `csptracker-postgres`) plus all the systemd user services were deployed by hand outside Coolify, so Coolify's dashboard does not give a complete picture of what's running on this box.
