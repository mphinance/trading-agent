# Expansion and Distribution Plan

*Prepared 2026-08-29. Contributed externally — kept as written.*

> **Editor's note (added 2026-08-29, after verifying against the tree).** The
> architectural thesis and the security/distribution phase hold up and have
> been folded into `ROADMAP.md`. But the "What exists today" section and parts
> of Phase 1–2 describe the **pre-migration** sidecar, not the current one —
> it reads as written against `CLAUDE.md`, which is stale on exactly this
> point. Deleted in `de60d51` and *not* present: `orders.py` (that role is now
> `vesper/execution_guard.py` alone), `server.py` + `static/` (no browser
> dashboard), `chat.py` (no browser chat). **There is no served HTTP API at
> all**; the only HTTP server is `vesper/bot/inbound.py`'s aiohttp webhook
> app, which nothing starts. So Phase 2's HTTP/`/api/v1` work is net-new
> build, not preservation.
>
> **Correction (same day, later):** `alerts.py`, `quotes.py`, `notify.py`,
> `watcher.py` and `stream.py` were restored later on 2026-08-29 and are live
> again — `list_alerts`/`arm_alert`/`cancel_alert` now have a real CLI/loop
> path behind them (`vesper.py alerts --arm ...`, evaluated by the watcher
> thread inside `vesper.py loop`), just not an HTTP one. See CLAUDE.md rules
> 4c/4d.
> See ROADMAP.md → Ideas Backlog → "From `docs/EXPANSION_AND_DISTRIBUTION_PLAN.md`"
> for the vetted subset and its priority order.

## Executive summary

This project is already more than a Claude dashboard: it is a trading and market-data service with a browser UI, an HTTP API, MCP tools, background monitoring, paper trading, and an OpenRouter integration inside Vesper.

The best direction is **not** to make every model talk directly to Webull. Instead:

> Keep one broker-independent application core, expose one canonical tool/API contract, and let Claude, OpenRouter, OpenAI-compatible clients, Telegram, Discord, and the browser use adapters around that core.

The order path must remain centralized in `orders.py` / `vesper/execution_guard.py`. No model adapter should hold broker credentials or implement its own risk checks.

## What exists today

- A Webull execution path with preview/confirm tickets and server-side caps.
- A mostly read-only browser dashboard.
- A Claude Desktop stdio MCP bridge.
- Vesper's direct OpenRouter chat-completions client in `vesper/llm.py`.
- A broker abstraction beginning to form around Webull and Public.com.
- Telegram and Discord approval channels.
- Paper ledger, strategy playbooks, alerts, monitoring, and a growing skills/MCP ecosystem.

The main architectural gap is that these surfaces are parallel integrations rather than one clearly named, versioned capability layer.

## Recommended target architecture

```text
                         Browser UI
                             │
Claude Desktop ── stdio MCP  │  OpenAI / OpenRouter / other clients
             │                │                │
             └───────────────┴────────────────┘
                         API + tool layer
                   (versioned, provider-neutral)
                             │
       ┌─────────────────────┼─────────────────────┐
       │                     │                     │
  Portfolio/read       Alerts/stream         Execution service
       │                     │                     │
       └─────────────── domain services ────────┘
                             │
                  broker adapters + data sources
                             │
                         Webull / others
```

### Core rule

Models choose *what to ask for*. The server decides *what is permitted* and *what actually happens*.

A model may request `preview_order`; it may never submit a raw Webull payload directly.

## Phase 1: make the current system model-neutral

### 1. Define a canonical tool contract

Create a versioned internal schema, for example `sidecar.tools.v1`, covering:

- `get_account_summary`
- `get_positions`
- `get_quote`
- `get_gamma_levels`
- `get_signals`
- `list_alerts`
- `arm_alert`
- `cancel_alert`
- `preview_order`
- `place_order` (ticket ID only)
- `replace_order`
- `cancel_order`
- `get_order_status`

The HTTP routes and MCP tools can map to this contract. Keep input/output schemas explicit and machine-readable. Generate or test `docs/API.md` from the same source where practical.

### 2. Separate domain services from adapters

Use thin adapters for:

- Claude Agent SDK
- OpenRouter chat completions
- OpenAI-compatible APIs
- MCP stdio
- Future MCP Streamable HTTP
- Browser chat

All adapters should call the same service functions and return the same structured results. Do not duplicate trading logic in an adapter.

### 3. Add an OpenRouter agent adapter, not a second broker integration

`vesper/llm.py` already proves that OpenRouter can be used. The next useful step is a small provider interface:

```python
class ModelProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, object]] | None = None,
        model: str | None = None,
    ) -> ModelResponse: ...
```

Implement:

- `ClaudeProvider` for the existing Agent SDK path
- `OpenRouterProvider` for OpenAI-style chat completions and tool calls
- `DeterministicProvider` for tests and offline operation

The provider returns requested tool calls; a separate tool runner validates and executes them. Never allow model-generated tool arguments to bypass server validation.

### 4. Start with read-only OpenRouter tools

First expose portfolio, quotes, signals, gamma, and alerts. Then add `preview_order`. Add `place_order` only after the adapter reliably handles the ticket handshake and explicit human confirmation.

This allows useful model experimentation without immediately increasing execution risk.

### 5. Add model capability profiles

Models differ in tool-calling reliability, context size, latency, and cost. Maintain a profile with fields such as:

- Supports tool calls
- Supports structured output
- Supports streaming
- Supports vision
- Maximum context
- Allowed environments
- Default risk tier

Use cheap/fast models for summaries and classification, stronger models for complex options analysis, and deterministic Python for safety-critical calculations.

OpenRouter's provider routing is useful for experimentation, but the application should record the selected model/provider in every response and audit event. Do not treat model availability or pricing as stable without refreshing metadata.

## Phase 2: make the API usable by other clients

### 1. Keep HTTP as the universal escape hatch

MCP is excellent for tool-aware hosts, but ordinary applications can use HTTP. Maintain a clean `/api/v1` contract with:

- OpenAPI schemas
- Stable error codes
- Idempotency keys for mutations
- Request IDs
- Explicit dry-run/live mode
- Structured audit events

This lets an OpenRouter agent, a custom Python script, a mobile app, or another model provider use the system without needing Claude Desktop.

### 2. Add a generic tool-description endpoint

Expose the canonical tool schemas in a format that can be converted into:

- OpenAI/OpenRouter `tools`
- MCP `tools/list`
- Function declarations for other SDKs

The schemas should describe permissions and confirmation requirements, not just argument types. For example, `place_order` should say that it accepts a `ticket_id`, not an order object.

### 3. Add streaming consistently

Use SSE for:

- Quotes
- Fills
- Order status
- Alert events
- Approval state changes

For remote MCP, evaluate the current MCP Streamable HTTP transport rather than inventing a custom long-lived protocol. Keep stdio for local Claude Desktop use. Remote access should not be enabled until authentication and tenant isolation exist.

### 4. Make mutations idempotent

Alerts, approvals, and order mutations should accept an idempotency key and return the original result when retried. This matters for model retries, network timeouts, and webhook redelivery.

## Phase 3: make it safely distributable

The current deployment model is appropriate for a trusted single operator, not a public SaaS product. Before distributing it to other users:

### Required security changes

1. **Authentication**
   - Use OAuth/OIDC or another established identity provider for remote users.
   - Use scoped access tokens, not one shared secret.
   - Follow MCP authorization guidance for remote MCP.

2. **Authorization**
   - Separate scopes such as `portfolio:read`, `alerts:write`, `orders:preview`, and `orders:execute`.
   - Require an explicit user identity for every approval.
   - Keep execution disabled by default for new installations.

3. **Tenant isolation**
   - Every account, ticket, alert, approval, audit event, and state file must be owned by a user/tenant.
   - Never use one process-global Webull credential set for multiple customers.
   - Prefer one isolated worker/process/container per connected brokerage account until multi-tenancy is proven.

4. **Credential handling**
   - Store broker credentials in an OS secret store or encrypted secret manager.
   - Never pass brokerage credentials to a model provider.
   - Never include tokens, account numbers, or raw broker payloads in logs or model prompts.

5. **Execution controls**
   - Preserve preview → human confirmation → placement.
   - Add per-user and per-account limits, cooldowns, daily loss/notional limits, and a kill switch.
   - Require recent confirmation for high-risk actions and option combos.
   - Audit who approved, which model proposed it, the exact payload hash, and the broker result.

6. **Network boundary**
   - Keep the current loopback/Tailscale default for personal deployments.
   - A public deployment requires TLS, authentication, rate limiting, CSRF protections where applicable, secure cookies/token handling, and hardened reverse-proxy configuration.
   - Never expose the current unauthenticated trading server to the public internet.

### Distribution options

#### Option A: Personal package first

Best near-term choice. Ship a documented installer, `.env.example`, health checks, paper mode, and local MCP/HTTP clients. Users run their own sidecar beside their own broker account.

#### Option B: Self-hosted multi-user package

Provide Docker/systemd deployment, persistent database migrations, auth configuration, backups, per-tenant secrets, and an admin audit view. This is a substantial security project, not just packaging.

#### Option C: Hosted SaaS

Highest effort and risk. Requires strict tenant isolation, encrypted credentials, broker consent flows, compliance review, support, abuse prevention, monitoring, and a clear responsibility model for real-money execution. Do this only after the self-hosted architecture is mature.

## High-value product ideas

### 1. Provider-neutral “AI cockpit”

Let the user select a model for each task:

- Fast model: quote/portfolio summaries
- Reasoning model: options scenarios and risk explanations
- Vision model: chart interpretation
- Deterministic engine: calculations and guardrails

Show the model/provider used, latency, and whether the answer came from live data or fallback data.

### 2. One-click paper mode

Make paper mode obvious in the UI and impossible to confuse with live mode. Include simulated fills, mark-to-market, realized/unrealized P&L, and a replayable audit trail.

### 3. Approval inbox

Unify browser, Telegram, Discord, and future clients around one approval state. A proposal should display:

- Exact symbol, side, quantity, price, and strategy
- Worst-case notional
- Buying-power impact
- Stop/target if applicable
- Model and rationale
- Payload hash
- Expiration time

### 4. Event-driven plugin system

Define safe plugin boundaries for:

- Market-data sources
- Signal providers
- Strategy/playbook generators
- Notification channels
- Model providers
- Broker adapters

Plugins should be read-only by default. A plugin cannot place trades unless it uses the central execution service and passes the same guards.

### 5. Replay and audit mode

Record sanitized inputs, tool calls, approvals, outputs, and broker responses. Allow a session to be replayed in paper mode. This is useful for debugging models and reviewing bad decisions without resubmitting orders.

### 6. Broker-neutral order model

Continue the existing broker abstraction, but define a canonical order schema independent of Webull. Broker adapters translate it into broker-specific payloads. Keep unsupported features explicit instead of silently approximating them.

### 7. Health and observability

Add structured metrics for:

- Broker/API latency and rate-limit events
- Quote freshness
- Alert evaluation lag
- Model latency and failure/fallback rate
- Tool-call rejection rate
- Approval age and expiry
- Paper/live order outcomes

Do not record sensitive payloads by default; use hashes and redacted summaries.

## Research notes and references

These are design inputs, not dependencies:

- [OpenRouter agent and tool documentation](https://openrouter.ai/docs/cookbook/building-agents)
- [OpenRouter API](https://openrouter.ai/docs/api-reference/overview)
- [Model Context Protocol transports](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)
- [Model Context Protocol authorization](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/authorization)
- [OpenAI remote MCP tools](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
- [FastAPI OAuth2 scopes](https://fastapi.tiangolo.com/advanced/security/oauth2-scopes/)
- [LiteLLM proxy documentation](https://docs.litellm.ai/docs/)

LiteLLM could be evaluated later as an optional gateway for teams wanting one OpenAI-compatible endpoint across many providers. It should not be added just to solve the first OpenRouter adapter: the repository already has a small direct OpenRouter client, and another gateway would add operational complexity.

## Suggested implementation order

1. Define and test canonical tool schemas.
2. Extract a provider-neutral model interface around the existing OpenRouter and Claude paths.
3. Add a read-only OpenRouter tool-calling client.
4. Reuse the same tool runner from HTTP, MCP, and browser chat.
5. Add idempotency, audit events, and paper-mode replay.
6. Add scoped authentication before any remote/public endpoint.
7. Add per-user credential isolation and persistent tenant state.
8. Only then consider remote MCP or hosted distribution.

## Bottom line

The “awesome” version is a **broker-safe AI cockpit**, not a Claude-specific bot:

- Any capable model can explain the account.
- Any compatible client can call the tools.
- Multiple model providers can be swapped without changing trading code.
- Deterministic Python remains in charge of calculations and risk.
- Humans remain in charge of real-money approval.
- Distribution comes after authentication and isolation, not before.
