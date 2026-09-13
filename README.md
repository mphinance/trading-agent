# Vesper

**A LangGraph trading agent for Webull that speaks MCP.** It scans for
setups, reads dealer-gamma structure off TraderDaddy Pro, drafts and
risk-gates an order, then asks you to approve it over **Telegram or
Discord** — chart attached — before anything touches your account.

- 🔌 **Two MCP servers built in.** `mcp_server/` is the portable one — 56
  read-only quant tools over stdio, no broker credentials, no order path.
  Plug it into **Claude Desktop**, **Claude Code**, **Codex CLI**, or anything
  else that speaks MCP. `trading_mcp/` is the owner-only one — **80 tools**,
  the extra 16 reading live account state, and three of those able to place an
  order. **New here?** [`docs/MCP_OVERVIEW.md`](docs/MCP_OVERVIEW.md) explains
  what the 80 tools are and how the safety model works;
  [`docs/TOOLS.md`](docs/TOOLS.md) is the full inventory.
- 🧩 **64 Claude Code skills** ship in `skills/` — VCP/CANSLIM screens,
  gamma/breadth/regime detectors, backtesting, a full edge-research
  pipeline, dividend SOPs, and more. Picked up automatically, zero setup.
- 🤖 **LangGraph pipeline** drafts the trade; a **deterministic risk gate**
  (not an LLM) enforces notional/quantity/buying-power caps before a human
  ever sees it.
- ✅ **Telegram/Discord approval** — preview → confirm ticket handshake,
  SHA-256'd payload, single-use, 120s TTL. Nothing reaches the broker
  without a human tap.

> **This is a single-operator personal tool, not a hosted product.** No
> multi-tenancy, no user model, no browser UI. There *is* one HTTP listener:
> `trading_mcp/server.py`, which binds the docker bridge `10.0.0.1:8500`
> behind Traefik at `https://agent.mphinance.com/mcp`, with a bearer token and
> OAuth 2.1 as the entire access gate. Both approval paths stay outbound-only
> — Telegram long-polls, Discord holds a gateway connection.
>
> **It can place real orders**; the kill switch (`VESPER_TRADING`) defaults
> **off**, and every agent-originated proposal needs a deterministic
> risk-gate pass and a human approval tap. See [CLAUDE.md](CLAUDE.md) for the
> full design rules.

**Needs a [TraderDaddy Pro](https://www.traderdaddy.pro) Developer API key.**
Dealer-gamma structure, most of the scanner's discovery (screeners, unusual
options flow, pre-market gappers, bounce signals), and the 0DTE playbook all
read live TDPro data — without `TD_API_KEY` set, those sources degrade
silently to nothing rather than crashing, and you're left with the free
yfinance/TradingView-backed VCP and squeeze screens. It's a standalone
subscription ($49.99/mo, or $29.99/mo alongside a TraderDaddy Pro platform
plan) — see [Credentials](#credentials).

## Contents

- [Documentation](#documentation) — start here if you're reading, not running
- [Connecting an MCP host](#connecting-an-mcp-host) — Claude Desktop, Claude Code, Codex
  - [The portable server](#the-portable-server-mcp_server)
  - [The owner-only server](#the-owner-only-server-trading_mcp)
- [Skills](#skills)
- [Run](#run)
  - [Credentials](#credentials)
  - [Getting started on a new machine](#getting-started-on-a-new-machine)
  - [Key environment variables](#key-environment-variables)
- [Layout](#layout)
- [Tests](#tests)
- [The order path](#the-order-path)
- [Deploy](#deploy)

## Documentation

| document | for whom |
| --- | --- |
| [`docs/MCP_OVERVIEW.md`](docs/MCP_OVERVIEW.md) | **Someone who's been sent the tool list.** What the 80 tools group into, what the Vesper half means, the five gates that bound an order, and why a tool can originate one but never approve one. No repo access assumed — this is the one to share. |
| [`docs/TOOLS.md`](docs/TOOLS.md) | The full 80-tool inventory, grouped by which credential each group needs. |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | **Someone taking over the repo.** First hour, where it runs, which env file is actually live, where state lives, the traps, and an honest list of what isn't finished. |
| [`CLAUDE.md`](CLAUDE.md) | **The design contract.** Seven critical rules — the order path, the kill switch, the LLM narrate/reject-only boundary, push-vs-poll for the monitor, dealer-gamma alert semantics — and why each exists. Authoritative; keep it current. |
| [`docs/CONNECTOR_AUTH.md`](docs/CONNECTOR_AUTH.md) | Where the token lives, why the bearer and OAuth credentials differ in what they can do, and how to reconnect the claude.ai connector when it 401s forever. |
| [`docs/GOTCHAS.md`](docs/GOTCHAS.md) | The estate-wide trap list — including ones that bite from a *different* repo than the one you're editing. |
| [`docs/API.md`](docs/API.md) | Both MCP surfaces, module by module. |
| [`docs/WEBULL_ORDER_PAYLOADS.md`](docs/WEBULL_ORDER_PAYLOADS.md) | Verified request shapes and the option-chain filtering gotchas. |
| [`deploy/README.md`](deploy/README.md) | Units, env contracts, Traefik, rollback runbook. |
| [`ROADMAP.md`](ROADMAP.md) | Status, known gaps, ideas backlog — including rejected ideas and why. |

A doc carrying a **superseded** banner is a historical record kept for the
reasoning, not current design. Trust the banner.

## Connecting an MCP host

There are two servers and they connect differently. `mcp_server/` is the one
you can hand to anyone; `trading_mcp/` is the one wired to a live account.

### The portable server (`mcp_server/`)

`mcp_server/` is a real MCP server — 56 read-only tools (screeners, technical
indicators, options/VoPR analytics, macro & breadth detectors, TraderDaddy
intel, backtesting, a knowledge-base search) — and it talks stdio by default,
so any MCP-compatible host can spawn it as a subprocess. `pip install -r
requirements.txt` is enough on its own — no separate `pip install -e .` step
needed, that used to be a real gap (`mcp_server/`'s own deps were declared in
`pyproject.toml` but never actually installed by `requirements.txt`, and
`mcp>=2` broke the pre-2.0 FastMCP API the code actually uses; both fixed).
It reads the same `./.env` as everything else; most tools need no extra key
at all (yfinance, TradingView, SEC EDGAR), 12 want `TD_API_KEY`, and
`OPENROUTER_API_KEY` covers the LLM-backed ones. It is safe to hand to any of
these — it never touches `vesper/execution_guard.py` and cannot place an order.

**Claude Desktop** — Settings → Developer → Edit Config:

```json
{
  "mcpServers": {
    "momentum": {
      "command": "/path/to/trading-agent/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/trading-agent"
    }
  }
}
```

**Claude Code** — from the repo root:

```bash
claude mcp add momentum --scope project -- /path/to/trading-agent/.venv/bin/python -m mcp_server.server
```

`--scope project` writes it to a `.mcp.json` at the repo root. That file isn't
gitignored here, so either commit it (absolute paths won't match a
collaborator's clone, so they'd need `--scope local` instead) or leave it
uncommitted and let each person run the command for themselves.

**Codex CLI** — in `~/.codex/config.toml`:

```toml
[mcp_servers.momentum]
command = "/path/to/trading-agent/.venv/bin/python"
args = ["-m", "mcp_server.server"]
```

`MCP_TRANSPORT=sse` is also supported (`mcp_server/server.py` will bind
`MCP_HOST`/`MCP_PORT` instead of stdio) for a host that needs a remote
connection rather than a local subprocess — nothing in this repo starts it
that way by default, and the code's allowed-hosts list is hardcoded to one
specific domain, so treat SSE mode as something to adapt, not use as-is.

### The owner-only server (`trading_mcp/`)

A separate process, and the one that's actually deployed. 80 tools, 65
`skill://` resources and 2 prompts, reachable over HTTPS rather than spawned as
a subprocess:

```
https://agent.mphinance.com/mcp
```

It adds 13 read-only views over live account and agent state on top of the 64
shared tools, plus 3 order tools. Those 3 sit behind an OAuth `trade` scope
that the static bearer token deliberately does **not** carry, so a long-lived
secret sitting in a file cannot place an order — only a token minted through
the human-present `/authorize` gate can.

One consequence looks like a broken deploy and isn't: a bearer `tools/list`
returns **77** tools, not 80, and calling an order tool with it answers
`Unknown tool` rather than 403, because MCP filters by scope. Don't "fix" that
by giving the bearer `trade`.

[`docs/CONNECTOR_AUTH.md`](docs/CONNECTOR_AUTH.md) is the operational guide —
including how to reconnect the claude.ai connector when it 401s forever.
[`docs/MCP_OVERVIEW.md`](docs/MCP_OVERVIEW.md) is what to send someone asking
what this server *is*.

## Skills

The repo ships 64 Claude Code skills under `skills/` — project-scoped, so
they're picked up automatically by any Claude Code session opened in this
directory, no registration step needed. A sample of what's in there:

| Skill | What it does |
| --- | --- |
| `stock-recap` | Runs every screener, pulls the biggest options flow and 13F activity, finds the names multiple independent sources agree on |
| `vcp-screener` / `canslim-screener` | Minervini VCP and O'Neil CANSLIM growth-stock screens |
| `momentum-squeeze` | Coiled-volatility scan → confirmed-uptrend pullback entries |
| `0dte-flow` | Same-day XSP/SPX decision support with hard risk guardrails |
| `market-breadth-analyzer` / `market-top-detector` / `ftd-detector` | Breadth health, distribution-day risk, follow-through-day bottom confirmation |
| `macro-regime-detector` | Cross-asset regime shifts (concentration, broadening, contraction, inflationary) |
| `institutional-flow-tracker` / `sector-analyst` / `theme-detector` | 13F flow, sector rotation, thematic lifecycle |
| `options-strategy-advisor` | Black-Scholes pricing, Greeks, strategy P/L simulation |
| `backtest-expert` | Parameter robustness, slippage modeling, bias prevention for strategy validation |
| `edge-*` (7 skills) | A full pipeline from raw observation → hint → concept → strategy draft → adversarial review → export |
| `kanchi-dividend-*` (3 skills) | Dividend-growth screening, forced-review triggers, US tax/account-placement rules |
| `breakout-trade-planner` / `position-sizer` | Entry/stop/target levels and portfolio-heat-aware sizing |

Every skill has its own `SKILL.md` describing what it's for and when to use
it — that's the full, current list; this table is a sample, not an index.
Invoke one by name (`/vcp-screener`, `/stock-recap`, ...) or just describe
what you want and Claude picks the matching skill.

## Run

```bash
./.venv/bin/python vesper.py scan             # scan for setups (VCP, squeeze, institutional flow, ...)
./.venv/bin/python vesper.py analyze NVDA     # deep technical + options audit for one ticker
./.venv/bin/python vesper.py 0dte             # SPY/QQQ 0DTE gamma-flip decision support
./.venv/bin/python vesper.py morning          # morning briefing
./.venv/bin/python vesper.py monitor          # one exit-cascade sweep over open positions
./.venv/bin/python vesper.py status           # halt state, circuit breaker, trading on/off
./.venv/bin/python vesper.py paper            # paper-trading ledger

./.venv/bin/python vesper.py listen           # long-poll Telegram for Approve/Reject/halt/resume taps
./.venv/bin/python vesper.py loop             # unattended: scheduled scans + continuous monitor + alert watcher
./.venv/bin/python vesper.py loop --live      # same, but drafts pause for remote approval — run `listen` too

./.venv/bin/python vesper.py alerts --arm SPY flip below   # arm a dealer-gamma alert
./.venv/bin/python vesper.py halt / resume                 # emergency freeze / release
```

`vesper.py --help` lists every command and flag. Nothing places an order
without `VESPER_TRADING=1` **and** an approval tap — `--live` only unlocks the
*attempt*, it does not skip the gate.

### Credentials

Two places, both gitignored:

| File | Contents |
| --- | --- |
| `./.env` (repo root) | `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `WEBULL_REGION_ID`, `WEBULL_ENVIRONMENT` (or `WEBULL_KEY`/`WEBULL_SECRET`) — **required**; `TD_API_KEY`/`TDPRO_API_KEY` — technically optional, but dealer-gamma reads and most scanner discovery need it, see above; `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` + `TELEGRAM_AUTHORIZED_USER_IDS` and/or `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` + `DISCORD_AUTHORIZED_USER_IDS` — at least one needed to approve anything; `OPENROUTER_API_KEY` (optional — narrative + risk audit only, see below) |
| `../.env.*` (one directory up) | the original per-service convention (`.env.notify`, `.env.telegram`, ...) — `notify.py` still reads both this and `./.env`, so either layout works |

`vesper.py` calls `load_dotenv()` on `./.env` at startup, which is why every
module can read straight from `os.environ`. An `export` in your shell does
**not** reach a systemd service — it needs its own env file.

Audit `git diff --cached` for `sk-ant-`, `td_live_`, or a bot token before any
push.

### Getting started on a new machine

```bash
git clone <this repo>
cd trading-agent
python3 -m venv .venv                              # 3.8-3.14 all fine on webull SDK 2.0.18
./.venv/bin/pip install -r requirements.txt

cp .env.vesper.example .env                         # then fill in real values
vi .env                                             # NEVER paste a placeholder through — generate:
                                                    #   openssl rand -hex 32

./.venv/bin/python vesper.py status                 # confirms Webull + TDPro connectivity
```

### Key environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `VESPER_TRADING` | off | The kill switch. Must be explicitly set truthy for any order to reach the broker. |
| `VESPER_MAX_NOTIONAL` | `2500` | Max $ per order, checked server-side on every path (a SELL-to-open option is sized off strike, not premium). |
| `VESPER_MAX_QUANTITY` | — | Max shares/contracts per order. |
| `VESPER_MAX_BP_FRACTION` | — | Cap an order at this fraction of buying power. |
| `VESPER_SYMBOL_ALLOWLIST` | — | Comma-separated. Empty means any symbol. |
| `VESPER_CIRCUIT_BREAKER_PCT` | 15% | Trailing-peak NLV drawdown that trips the emergency halt automatically. |
| `OPENROUTER_MODEL` | — | Model used for thesis narrative + risk red-team (rule 6 in CLAUDE.md — narrate/reject only, never originate or upsize). |

The MCP order tools sit behind a *second*, stricter set of bounds, enforced at
the single staging chokepoint every path shares:

| Variable | Purpose |
| --- | --- |
| `MCP_MAX_NOTIONAL` | Absolute $ ceiling for one MCP-originated order. |
| `MCP_MAX_NOTIONAL_PCT` | Fraction of NLV for one MCP-originated order. The operative cap is `min()` of the two, and it **fails closed** — if NLV can't be read the cap is 0 and every opening order is refused. Closing orders skip it; they can't increase exposure. |
| `MCP_MAX_DAILY_ORDERS` | Orders per day from the MCP surface. |
| `TRADING_AGENT_TOKEN` | Static bearer for the owner MCP server. Carries `read` + `safe-write`, deliberately **not** `trade` — a long-lived secret on disk cannot place an order. Generate it (`openssl rand -hex 32`); the server refuses to start on a placeholder or low-entropy value. |

## Layout

```
vesper.py          CLI entrypoint: scan / analyze / 0dte / morning / monitor /
                    loop / listen / alerts / halt / resume / status / paper / audit
vesper/
  graph.py          LangGraph pipeline + disk-backed SQLite checkpointer
  runner.py         Drives one agent session
  loop.py           Unattended daemon: scheduled scans + monitor + alert watcher
  state.py          Pydantic models (OrderProposal, OrderLeg, TradingState, ...)
  execution_guard.py  THE ORDER PATH — the only module that can move money
  risk.py           RiskEnforcer: sizing + capital-allocation buckets
  halt.py           Thin compat re-export of core/halt.py, kept only because
                    execution_guard.py (never edited) imports from here
  monitor.py        Position monitor + exit cascade
  llm.py            OpenRouter: thesis narrative + risk red-team, narrate/reject only
  agents/           Specialist swarm: technical, flow, fundamental, gamma,
                    synthesis/debate supervisor, adversarial risk
  nodes/            regime, scanner, analyst, swarm_node, playbooks, synthesis_node,
                    risk_gate, human_gate, executor, reflection (the actual edge order)
  bot/              Telegram + Discord approval adapters, channel manager
  brokers/          public_broker.py (second, partial adapter)

core/               Shared layer both vesper/ and trading_mcp/ import from
  wb.py             Webull client — credentials, account/order reads, the scarce 2-req/2s bucket
  md.py             Market data, research, screeners (separate 600/min bucket — don't merge with wb.py)
  td.py             TraderDaddy Pro client + dealer-gamma compaction (td.levels())
  halt.py           Emergency freeze, checked before anything else
  circuit_breaker.py  Trailing-peak NLV drawdown -> automatic halt
  secret_hygiene.py   Refuses a placeholder or low-entropy credential (rule 2)
alerts.py           Alert store + crossing logic (a level can BE dealer structure)
watcher.py          Background thread evaluating alerts
notify.py           Alert delivery: ntfy and/or Telegram
stream.py           MQTT quote push + gRPC trade-event push, wakes the monitor on a fill
mcp_server/         Quant tooling exposed over MCP (FastMCP, stdio) — screeners, backtests,
                    options analytics. 56 tools, no broker credentials, no order path.
trading_mcp/        Owner-only MCP server, SEPARATE process — 80 tools, 65 skill
                    resources, 2 prompts. Deployed behind Traefik. See docs/TOOLS.md.
tests/              pytest, hermetic — Webull and Agent SDKs stubbed in conftest
deploy/             Three systemd user units + two env contracts + Traefik config
docs/               MCP_OVERVIEW.md (external explainer), HANDOFF.md (onboarding),
                    TOOLS.md (80-tool inventory), CONNECTOR_AUTH.md, GOTCHAS.md,
                    API.md, vendored Webull OpenAPI reference
ROADMAP.md          Single planning doc: status, known gaps, ideas backlog
```

There is no `server.py` and no browser UI — an earlier version of this repo
had those, and they were deliberately removed rather than kept around unused.

The two MCP packages are different things and the difference is load-bearing.
`mcp_server/` exposes quant tooling (screeners, technicals, backtests) to MCP
hosts over stdio; it holds no broker credentials and has no order path, and
that property is deliberate. `trading_mcp/` is a separate process that *does*
hold them: it adds 13 read-only views over live account and agent state, plus
three order tools behind an OAuth `trade` scope. Even there, the order tools
reach the broker only through `vesper/execution_guard.py` and duplicate no
risk check of their own — and `resume()` and `submit_decision()` are
unreachable from every MCP module, so a tool call can originate an order but
can never approve a pending one.

## Tests

```bash
pip install -r requirements-dev.txt && pytest -q
```

Hermetic — no network, no broker, no credentials. The Webull SDK and Agent SDK
are stubbed in `tests/conftest.py` (one needs a compiler and pins the Python
version, the other shells out to an npm-only binary), so
`requirements-dev.txt` is deliberately **not** a superset of
`requirements.txt`. An autouse fixture redirects every on-disk state file
(halt, circuit breaker, paper ledger, approval registry, graph checkpoints) to
a temp dir, so a test run cannot touch real state or your account.

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.12 and 3.13, plus a
`compileall` pass and a credential-shaped-string scan, on every push and PR.

## The order path

`vesper/execution_guard.py` is the only module allowed to write to the broker.
**Preview, then confirm, then place**: `preview()` runs the guards and stages a
ticket carrying a SHA-256 of the exact payload; `place()` takes a `ticket_id`,
never a raw order, so what was approved is byte-for-byte what reaches Webull.
Tickets are single-use and expire in 120 seconds. Approval happens on a
Telegram or Discord card, not a spoken or typed command — see CLAUDE.md rule
4d for why voice specifically never confirms an order.

![A Telegram proposal card: a daily+5m composite chart, ticker, action, estimated cost, max risk, stop loss, target, proposal digest, time stop, and Approve/Reject buttons](docs/screenshots/Screenshot_20260829-191404.png)

*A test proposal — the chart, digest, and Approve/Reject buttons are real; the
numbers are not a live account.*

**Not exercised against a live account yet.** The order path is tested end to
end against a stub broker, which proves the wiring, not Webull's acceptance of
it. The request *payload shapes* are verified — established by sending real
`preview_order` / `preview_option` calls, which are non-committal — and that
caught three genuine errors in the single-leg path. Multi-leg combos beyond a
single option leg are refused at the executor on purpose: the correct strategy
enum is unverified, and a guessed payload to a live order endpoint is not an
acceptable way to find out. See the Status section of [CLAUDE.md](CLAUDE.md)
for exactly what has and hasn't been verified live.

## Deploy

Deployment is managed via three systemd user services under `deploy/`:
- `trading-agent.service` (owner MCP server, bound to docker bridge `10.0.0.1:8500`)
- `vesper-loop.service` (autonomous scan and monitor loop)
- `vesper-listen.service` (inbound webhook and approval listener)

`trading-agent.service` is the one that's actually live. Traefik terminates
TLS in front of it at `https://agent.mphinance.com/mcp`; the bind is the docker
bridge rather than loopback because Traefik is containerised and can't reach
the host's loopback. `MCP_HOST` defaults to `127.0.0.1`, so widening it is
always an explicit act.

Configuration is split into `.env.trading-agent` and `.env.vesper`. **Which
file is live is not obvious and getting it wrong has already cost a day of
production exposure** — a bare `.env` on a deployed box is read by nothing, so
editing it to rotate a credential changes nothing the service sees. Read
`deploy/README.md` before touching either, and generate every secret rather
than copying one: `install.sh` refuses to deploy while any credential still
equals its `.example` value.

---

Everything else is in [Documentation](#documentation) above.
[CLAUDE.md](CLAUDE.md) is the one to read before changing anything.
