# Discord stack on the coolify box — read-only survey

Investigated 2026-09-02 via `ssh coolify` (read-only: no start/stop/restart/deploy
performed). Two existing Discord bots live on this box, both under user `mph`.
Findings below, followed by a recommendation for how `trading-agent` (a third,
separate project on the same box) should post an Approve/Reject card and act on
the button press.

**Credential handling note:** every env var below is reported by NAME only.
Where a value was pulled from `.env`/`docker inspect`, it was a numeric Discord
guild/channel/user ID (not a secret — Discord IDs are public identifiers, and
the task brief explicitly calls for reporting them) or was empty in the output.
No token, key, password, or webhook URL value is included anywhere in this file.

---

## 1. disclaw (in `~/nyx`)

**Path:** `/home/mph/nyx/disclaw` (symlinked at `/home/mph/disclaw`).
**Repo:** fork of the open-source `six-ddc/disclaw` project.

### What it is
A Discord ↔ Claude Code bridge: `@mention` the bot in a channel, it opens a
thread, spawns a Claude Agent SDK session (`bun`-executed
`@anthropic-ai/claude-agent-sdk` CLI) scoped to a working directory, and
streams the session back into Discord as rich embeds — tool calls, diffs,
plan review, Q&A, and **tool-permission approval** all render as Discord UI
(buttons/selects/modals). It is Michael's general-purpose "Claude Code from
your phone" bot, not a trading-specific bot, but its tool-approval flow is the
closest existing analogue in either project to an Approve/Reject trading card.

### How it runs
- systemd **user** service: `disclaw.service`, enabled, currently **active
  (running)**, started 2026-09-02 10:58 EDT (~4.5h uptime at inspection time).
  Main process: `bun run src/bot.ts` (PID 3802074), which itself spawns a
  per-active-session `bun .../claude-agent-sdk/cli.js` child while a
  conversation is in flight.
- Systemd restarts it on crash (standard unit semantics); `service/manage.sh`
  and a `.plist`/`.service.template` exist in-repo for install, but the live
  config is the already-installed user unit.

### Discord integration in detail
- **Library:** `discord.js` v14.25.1 (from `package.json`), TypeScript, Bun
  runtime. Also pulls in `@discordjs/voice` for a voice-channel feature.
- **Connection:** persistent Gateway websocket (`new Client({ intents: [...] })`
  in `src/bot.ts`), not webhook-only. Intents: `Guilds`, `GuildMessages`,
  `MessageContent`, `GuildMessageReactions`, `GuildVoiceStates`.
- **Interactive components:** buttons, select menus, and modals are used
  extensively — tool-approval (Allow/Deny/Always-Allow), plan review
  (Accept Edits/Manual Approval/Keep Planning), AskUserQuestion
  (button-or-select with Submit-All), a visual directory picker, cron control
  panels, and a paginated "pager" for long tool output.
- **Two different persistence models coexist in the same codebase**, and the
  distinction matters for the recommendation below:
  - **Ephemeral, in-memory** (`src/user-input.ts`): approval/plan/Q&A prompts
    are tracked in a `pendingRequests: Map<requestId, ...>` created fresh per
    request, with a `customId` of the form `approve:{requestId}:{action}` /
    `plan:{requestId}:{action}` / `ask:{requestId}:{action}`. This map is
    **not backed by disk** — a bot restart loses every pending approval (the
    request effectively expires; nothing resumes it).
  - **Persistent, DB-backed** (`src/cron-buttons.ts`, `src/tool-pager.ts`):
    cron control buttons use `customId` format `cron:{jobId}:{action}` where
    `jobId` is a stable ID stored in SQLite — the handler re-derives the job
    from the DB on every click, so it survives a restart even though nothing
    is explicitly "re-registered." Per the README, pager messages restore
    their navigation buttons on a stale reaction "backed by SDK session data,
    not in-memory state," i.e. also restart-safe.
  - There is **no discord.js-native persistent `View` re-registration** here
    (that's a discord.py concept); discord.js just dispatches
    `interactionCreate` by `customId` to whatever handler is running, so
    "restart-safety" in this codebase is entirely a matter of whether the
    handler's state lookup hits a DB/file or an in-memory Map.
- **Real approve-button snippet** (`src/user-input.ts`):
  ```ts
  async function handleApproveButton(interaction: ButtonInteraction): Promise<boolean> {
      const parts = interaction.customId.split(':');
      // approve:{requestId}:{action}
      if (parts.length < 3) return false;
      const requestId = parts[1]!;
      const action = parts[2]!;
      const request = pendingRequests.get(requestId);
      if (!request || request.type !== 'approval') {
          await interaction.reply({ content: 'This approval has expired.', ephemeral: true });
          return true;
      }
      if (action === 'allow') {
          pendingRequests.delete(requestId);
          clearTimeout(request.timeout);
          await interaction.deferUpdate();
          await disableComponents(request, `Allowed by ${interaction.user.tag}`);
          request.resolve({ behavior: 'allow', updatedInput: request.toolInput });
          return true;
      }
      ...
  }
  ```
- **Authorization on the approval buttons themselves:** **none found.** The
  handler above never checks `interaction.user.id` — anyone who can see and
  click the button in that thread can Allow/Deny/Always-Allow a tool call.
  Access control is implicit (whoever is in the server/channel), not an
  explicit allowlist. The one place an explicit owner check *does* exist is
  `src/sleeper.ts:336`: `if (OWNER_ID && interaction.user.id !== OWNER_ID)`,
  gated by the `SLEEPER_OWNER_ID` env var — but that's for a separate
  "sleeper" feature, not the general approval flow.
- **Guild/channel IDs** (from `.env`, values are non-secret Discord snowflakes):
  - `DISCLAW_VOICE_GUILD_ID=1523084544440271068`
  - `DISCLAW_VOICE_CHANNEL_ID=1541616749772931103`
  - `DISCLAW_AMBIENT_CHANNELS=1527845416408711180`
  - `AGENT_RUNS_CHANNEL_ID=1533580491331080414`
  - `SLEEPER_CHANNEL_ID=1542724307463966801`, `SLEEPER_OWNER_ID=350718254584561666`
    (a Discord user ID, presumably Michael's)
  - `DISCLAW_HUBS` maps multiple guild→channel-list pairs for a "hub fan-in"
    feature.
- **Rate-limit handling:** relies on `discord.js`'s built-in queueing/backoff
  for the Discord API (explicitly noted in a code comment in `src/discord.ts`:
  "rate limit handling, queuing, and retry logic for free"). Separately,
  `src/runner.ts` has its own exponential backoff (`backoffBaseMs * 2^attempt`)
  for retrying **Claude API** overload errors (429/503/529) — unrelated to
  Discord rate limits.
- **Env vars present** (names only): `DISCORD_BOT_TOKEN`, `ANTHROPIC_API_KEY`,
  `OPENROUTER_API_KEY`, `SUPABASE_ACCESS_TOKEN`, `AGENT_RUNNER_SECRET`, plus
  the channel/guild/dir/model config vars listed above.

### Data layer
SQLite only, at `~/.local/share/disclaw/threads.db` (~828 KB) — thread→session
mappings, channel working-dir config, cron job definitions. Per the project's
own README: "No message content, user data, or conversation history is
persisted." Logs are flat files under `~/.local/state/disclaw/logs/`.

### Reusable pieces for a separate project
- `src/discord.ts` — thin Discord-send/edit wrapper (`sendRichMessage`,
  `editRichMessage`) that already handles rate limits and message splitting;
  could be copied as a pattern, but it's tightly coupled to this bot's own
  embed/pager types.
- The `approve:{requestId}:{action}` button shape in `src/user-input.ts` is a
  clean template for a Yes/No card, but its **in-memory** `pendingRequests`
  map is exactly the property to *not* copy for a trading Approve/Reject card
  (an order approval that silently expires on a bot restart is a correctness
  bug, not just a UX one).
- No existing cog/plugin seam: disclaw is a single monolithic `bot.ts` process
  wiring everything together at startup — there's no "add a handler" extension
  point analogous to a discord.py Cog.

---

## 2. TraderDiscord-v2

**Path:** `/home/mph/TraderDiscord-v2`.

### What it is
A much larger, production Discord community platform for TraderDaddy Pro (a
market-data product): "Mission Control" for a professional options-trading
Discord server. Routes market signals/options-flow/alerts into channels
(IFTTT-style router), runs an LLM persona ("Sam AI"), serves 18 `/td` boards
(interactive multi-page embeds: signals, movers, unusual activity, earnings,
gamma exposure, etc.), plus full moderation (auto-mod, raid detection,
impersonation detection, reaction roles, support tickets) and a web dashboard.
Confirmed **currently running** (see below) — this is the live bot behind the
actual TraderDaddy Pro Discord server.

### How it runs
Docker Compose, deployed via Coolify. Three live containers matched from
`docker ps`:
| Container | Image/role | Status | Restart policy | Started |
|---|---|---|---|---|
| `bot-e10bttp3ewl6jinj08606h5q-*` | Discord bot + 23-endpoint mgmt API, port 8300 | Up 4 days (healthy) | `unless-stopped` | 2026-08-29 00:32 UTC |
| `ingest-e10bttp3ewl6jinj08606h5q-*` | FastAPI ingest/router/dashboard API, port 8200 | Up 4 days (healthy) | `unless-stopped` | 2026-08-29 00:32 UTC |
| `yt-notify-e10bttp3ewl6jinj08606h5q-*` | YouTube-upload → Discord notifier | Up 4 days | `unless-stopped` | 2026-08-29 00:32 UTC |

(A fourth compose service, `scraper`/`convoy` on 8400/5005, is defined in
`docker-compose.yml` but not currently in `docker ps`'s running set at
inspection time — not otherwise investigated further, out of scope.)

Also on the **host** (not in Docker): `nyx_bridge/server.py`, a small
`ThreadingHTTPServer` (PID 3296, running since Aug 25) that lets the in-container
Sam bot ask a host-side `claude -p` (read-only, `Read/Grep/Glob/git log|diff|show`
only) questions about its own source code — a debugging aid, not a
control/approval channel. Not directly reusable for an order-approval flow.

### Discord integration in detail
- **Library:** `discord.py` 2.5.2 (pinned in `bot/requirements.lock`), Python
  3.12, running inside the `bot` container.
- **Connection:** persistent Gateway websocket via `commands.Bot`
  (`TraderDiscordBot(commands.Bot)` in `bot/bot.py`), intents:
  `default()` plus `message_content`, `reactions`, `guilds`, `members`,
  `presences`.
- **Interactive components:** buttons, select menus, modals throughout —
  self-role buttons/selects, support-ticket buttons, and (most relevant here)
  a **restart-proof paginated board system**.
- **Restart-survival is explicit and load-bearing**, and is the strongest
  pattern found on this box. Two mechanisms, both in `bot/bot.py`'s
  `setup_hook`:
  1. **Self-role buttons** — persisted config in `button_roles.json`,
     re-hydrated into `discord.ui.View(timeout=None)` objects and
     re-registered with `self.add_view(view, message_id=int(msg_id))` on every
     boot (the classic discord.py persistent-view pattern):
     ```python
     # Re-register persistent button/select views so they survive restarts
     for gid, messages in all_br.items():
         guild = self.get_guild(int(gid))
         for msg_id, cfg in messages.items():
             view = discord.ui.View(timeout=None)
             for m in mappings:
                 view.add_item(SelfRoleButton(role_id=m["role_id"], ...))
             self.add_view(view, message_id=int(msg_id))
     ```
  2. **The `/td` boards** (`bot/board_views.py`) go a step further and don't
     need any registry replay at all — they use `discord.ui.DynamicItem` with
     a regex-matched `custom_id`, registered once by *class* via
     `bot.add_dynamic_items(BoardButton)`, so Discord routes clicks on
     **any** old message straight to fresh Python state reconstructed purely
     from the `custom_id` string — no DB lookup, no in-memory map, no replay
     list needed:
     ```python
     class BoardButton(
         discord.ui.DynamicItem[discord.ui.Button],
         template=r"tb\|(?P<board>[a-z]+)\|(?P<action>[a-z]+)\|(?P<page>-?\d+)\|(?P<args>.*)",
     ):
         """... no in-memory state, so it keeps working across view timeouts
         and bot restarts."""
         @classmethod
         async def from_custom_id(cls, interaction, item, match, /):
             return cls(match["board"], match["action"], int(match["page"]),
                         match["args"], label="​")
     ```
     The module's own docstring explains why: the previous version used
     5-minute-timeout in-memory views that died both on timeout and on every
     deploy, surfacing as a raw Discord "This interaction failed" — this
     rewrite (`custom_id` = `tb|{board}|{action}|{page}|{args}`) refetches
     live data on every click instead of caching state, so it's both
     restart-proof and never stale.
- **Authorization:** no in-code Discord-user-ID allowlist was found. The
  moderation slash-command group instead declares
  `default_permissions=discord.Permissions(administrator=True)`
  (`bot/cogs/mod_commands.py:199`), which pushes the access decision to
  Discord's own per-guild slash-command permission UI (server admins choose
  who/which roles can invoke it) rather than an application-level check.
  Regular `/td` board buttons appear to have no restriction beyond channel
  visibility — same implicit model as disclaw.
- **Guild/channel IDs:** `TD_PRO_GUILD_ID = 1480917210313789482` is hardcoded
  in `bot/bot.py` (used for slur-list seeding and guild-scoped checks in
  several places). `OPS_ALERTS_CHANNEL_ID` is a container env var but came
  back empty from `docker inspect` at the time of this check — either unset
  on this deployment or not currently wired to a live value.
- **Rate-limit handling:** relies on `discord.py`'s built-in HTTP rate-limit
  handling (no custom Discord-API backoff/queue code found). The "rate
  limit"/"backoff" hits found in the codebase are all for **LLM providers**
  (OpenRouter/Gemini usage counters in `sam_ai.py`), not the Discord API.
- **Env vars present on the `bot` container** (names only, via `docker
  inspect`): `DISCORD_BOT_TOKEN`, `BOT_API_KEY`, `HMAC_SECRET`,
  `MUR_SIGNING_SECRET`, `NYX_BRIDGE_TOKEN`, `MOD_DB_PASSWORD`, `ADMIN_PASS`,
  `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `CHART_IMG_API_KEY`,
  `TRADERDADDY_JWT_TOKEN`, `TRADERDADDY_EMAIL`, `TRADERDADDY_PASSWORD`,
  `TRADERDADDY_API_URL`, `YT_DISCORD_WEBHOOK`, `WHEEL_TRACKER_DB_PATH`,
  `OPS_ALERTS_CHANNEL_ID`, `SAM_TOOLS_ENABLED`, plus Coolify-injected
  `COOLIFY_*`/`SERVICE_NAME_*` metadata.

### Data layer
A single Docker named volume (`e10bttp3ewl6jinj08606h5q_bot-data`, mounted at
`/data` in the `bot` container) holding **~484 MB** total: several SQLite
databases in WAL mode (`activity.db`, `member_activity.db`, `mod.db`,
`conviction.db`, `traderdaddy_history.db`, `wheel_tracker.db`), a `chromadb/`
directory plus a hand-rolled `vecstore/` (the code comments note
`chromadb` 1.5.9 segfaults on large HNSW indexes on this box, so parts of the
vector store were reimplemented around it), and assorted JSON config/state
files (`reaction_roles.json`, `watchlists.json`, `live_boards.json`, etc.). Not
inspected for row content per the read-only/no-sensitive-data-dump instruction.
Separately, `docker-compose.yml` defines a Postgres+Redis+PgBouncer stack for
"Convoy" (legacy webhook delivery) — not confirmed running at inspection time.

### Reusable pieces for a separate project
- **`bot/board_views.py`'s `DynamicItem`/`custom_id`-template pattern** is the
  single best reusable idea on this box for an Approve/Reject card: encode
  `{action}|{order_id}` (or similar) directly in the `custom_id`, register the
  button *class* once via `add_dynamic_items`, and every click — even on a
  message from before the last restart — resolves via regex match with zero
  replay bookkeeping. This is a pattern to imitate in a new bot, not
  literally shared code (it's bound into `TraderDiscordBot`).
  `bot/bot.py`'s `setup_hook`'s persistent-`View`-replay-from-JSON pattern is
  the second option if state needs to live in a lookup table instead of the
  `custom_id` itself.
- No existing cog is order/approval-shaped; the closest conceptually is
  `bot/board_views.py`'s `detail()` per-ticker ephemeral card, which is a
  read/display pattern, not an act-and-mutate-state one.
- The bot process itself (`TraderDiscordBot`) is a plausible host for a new
  cog (discord.py cogs are `bot.add_cog(...)`-loadable units and this bot
  already loads several), but see the recommendation below for why that's not
  favored here.

---

## 3. Comparison and recommendation

**Question:** `trading-agent` (separate Python project, not part of either
repo above) needs to POST an interactive Approve/Reject card to Discord and
ACT on the button press (per its own `rule 3`/`execution_guard.py` design —
preview → ticket → human approval → place).

### Options weighed

1. **Add a second bot, own token, own gateway connection.**
   - Risk: none of the "two gateway connections on one token" problem applies
     — a *new* bot application gets its own token, so there's no session
     invalidation risk from sharing. Cost is real but small: one more Discord
     application to create/invite, one more token to store
     (`trading-agent/.env` already has a place for exactly this per its own
     `CLAUDE.md` rule 2), one more always-on process.
   - This keeps `trading-agent`'s only Discord-capable code inside
     `trading-agent`'s own deploy lifecycle — consistent with rule 3's "the
     order path lives in exactly one file, in exactly one repo's trust
     boundary" posture already documented in this project's `CLAUDE.md`.

2. **Add a cog/handler to an existing running bot (disclaw or
   TraderDiscord-v2), reusing its gateway connection.**
   - disclaw: general-purpose Claude-Code-via-Discord bot with **no
     authorization check on its approval buttons** and an **in-memory-only**
     pending-approval model that loses state on restart. Both are wrong
     properties for an order approval: this repo's own rule 4d already
     insists "buttons move money" must be per-user authorized and
     restart-safe; disclaw's approval flow is neither, today.
   - TraderDiscord-v2: has the *right* restart-safe button pattern
     (`DynamicItem`+`custom_id`), but it is someone else's production
     community bot serving a paying community, moderation, and an LLM
     persona. Adding a trading-order-approval cog would couple
     `trading-agent`'s risk-relevant code to that bot's deploy cadence,
     crash surface (it already touches a segfault-prone chromadb, a large
     `bot.py`, dozens of cogs), and blast radius — a bug in an unrelated cog
     restarting that container would interrupt a pending order approval.
     `trading-agent`'s own `CLAUDE.md` rule 3 is explicit that the order path
     "is a new threat model, not a small addition" for *any* adapter that
     grows its own order path — piggybacking on someone else's bot process is
     exactly that.
   - Neither bot's process is one `trading-agent` owns or deploys, so neither
     satisfies rule 2/rule 3's secrets-and-trust-boundary posture without
     real rework (adding an owner-only allowlist check, moving to
     restart-safe buttons, and taking on the other bot's operational risk).

3. **Webhook only.** Ruled out outright: an incoming webhook can post a
   message with buttons attached, but Discord will not deliver the resulting
   `INTERACTION_CREATE` button-click event to anything listening only via
   webhook — receiving an interaction requires either a live Gateway
   connection or a registered HTTP Interactions Endpoint URL. `trading-agent`
   is explicitly loopback/Tailscale-only with no exposed port (rule 1), which
   rules out the HTTP-interactions-endpoint variant too (it would need a
   public HTTPS URL Discord can reach). So this option is not viable at all
   for the "ACT on the button press" half of the requirement.

4. **Run its own separate gateway connection (same as option 1, different
   phrasing in the brief).** Functionally identical to option 1 once a new
   bot token exists — a gateway connection is per-token, so "new gateway
   connection" and "new bot" are the same commitment here.

### Recommendation

**Add a second, dedicated Discord bot with its own token and its own gateway
connection, living inside `trading-agent`.** Concretely: a small discord.py
(or discord.js, to match whichever the operator prefers to maintain) process
started the same way `vesper/bot/*`'s Telegram/Discord approval channels are
today, using the **`DynamicItem` + `custom_id`-encodes-the-ticket-id** pattern
demonstrated in `TraderDiscord-v2/bot/board_views.py` (imitate the pattern,
don't import the code — it's bound to that bot's class) so an Approve/Reject
click resolves correctly even if `trading-agent`'s bot process restarted
between posting the card and the human clicking it. Add an explicit
`interaction.user.id` allowlist check before acting on the click — modeled on
`disclaw/src/sleeper.ts`'s `OWNER_ID` check, since neither bot's *approval*
buttons have that check today and this project's rule 4d requires it.

This is favored over reusing either existing bot because: disclaw's approval
UI is unauthenticated and non-persistent (wrong on both axes this task cares
about), and TraderDiscord-v2 is a large, actively-changing production bot for
an unrelated paying community whose deploy lifecycle and crash surface
`trading-agent`'s order-approval path has no business depending on — exactly
the "new threat model" this repo's own CLAUDE.md already warns against for
any new order-adjacent surface. A brand-new bot costs one more token, one more
Discord application, and one more small always-on process — small, contained,
and fully within `trading-agent`'s own trust boundary.

**Could not determine:**
- Whether `TraderDiscord-v2`'s `scraper`/`convoy` compose services (Postgres/
  Redis/webhook delivery) are currently running — they weren't in the
  `docker ps` output at inspection time, but weren't independently confirmed
  stopped either; out of scope for this survey.
- The exact reason `OPS_ALERTS_CHANNEL_ID` came back empty from `docker
  inspect` on the `bot` container (unset vs. redacted vs. not applicable to
  this deployment) — not pursued further since it's not load-bearing for the
  recommendation.
