# Status — what is done, what is left

Generated 2026-09-02. Source of truth is `autonomous/feature_list.seed.json`; `feature_list.json` is the working copy the agents edit and it gets clobbered mid-run, so re-merge from the seed.

**21 of 105 features complete.** Test suite: 597 passing.

## Verified working right now

- `https://agent.mphinance.com/mcp` — TLS valid, 60 tools, MCP handshake succeeds, connector attached to Claude. Unauthenticated requests get 401.
- The MCP server no longer loads Vesper's runtime: all 13 Vesper tools invoked, **zero `vesper.*` modules** enter `sys.modules`. `execution_guard` and the bot adapters are out of that process entirely.
- CI green on 3.12 and 3.13.
- Bound to `10.0.0.1:8500`, never `0.0.0.0`; ufw allows only `10.0.1.0/24`.

## Known gaps that are not features

- **An unexplained Vesper instance** sent execution reports (paper mode) from somewhere not on any known host. Bot deleted, token removed, so it is silenced but not located.
- `sam-dashboard:8400` and Traefik's dashboard `:8080` are exposed to the internet past ufw via Docker's FORWARD-chain bypass. See `docs/COOLIFY_MAP.md`.
- coolify is at 77% disk with 2.2GB swap in use.

## M1 — Repo cloneable again — COMPLETE (7/7)

## M0 — The core/ split — COMPLETE (10/10)

## M2 — OAuth 2.1 on the MCP server (2/11)

- `M2-03` The uncommitted trading_mcp/oauth_provider.py and its trading_mcp/server.py diff are reviewed and their disposition -- finish, keep-as-is, or discard -- is decided and acted on
- `M2-04` An OAuth 2.1 authorization server is mounted on the MCP app, with the standard discovery endpoints returning well-formed metadata
- `M2-05` Dynamic client registration works end to end: a client registers, completes the authorization code exchange, and the resulting token is accepted by the MCP endpoint
- `M2-06` Both the static-bearer and OAuth paths converge on exactly one authorization-decision function, and an OAuth handshake cannot escalate beyond the scopes it was granted
- `M2-07` The permanently forbidden actions (guard.preview/place, submit_decision, resume) have no corresponding MCP tool registered at all, under any OAuth scope -- testable today against the existing tool set, with no new tool code required
- `M2-08` The static bearer fallback keeps working unchanged after OAuth lands, and the http transport still refuses to start without TRADING_AGENT_TOKEN
- `M2-09` OAuth tokens are revocable and stored server-side outside git, 0600, and the storage path is registered in the test suite's isolated-state fixture
- `M2-10` oauth_provider.py and any other new OAuth module has zero import-time coupling to vesper's runtime and is covered by the exposure-boundary AST pin
- `M2-11` An unauthenticated request to /mcp returns 4xx on the live coolify box, and the Traefik dynamic config matches whatever routes OAuth added

## M8 — Voice as setup co-pilot (0/18)

- `M8-01` watch_setup(proposal_id) returns one small, speakable payload: thesis, entry/stop/target, current price, distance-to-trigger in both % and $, a worded 5-minute structure summary, VWAP relation, and nearby compacted dealer-gamma levels
- `M8-02` The 5-minute structure summary is a pure, independently-tested function that renders bar structure in spoken words -- consecutive higher/lower lows, range direction, volume vs the 20-bar average -- never a raw bar dump and never a wrapped chart image
- `M8-03` Gamma levels in watch_setup always come from core.td's compacted levels() shape (never get_gex_ticker's raw ~40KB payload), flip_split is surfaced rather than silently resolved when the flip sources straddle spot, and no summary sentence phrases a level as a price target
- `M8-04` A repeated watch_setup call within a short window, with nothing materially changed, returns a compact unchanged response instead of restating the full thesis and structure summary, while the headline distance-to-trigger number is always present
- `M8-05` find_pending_setup(query) fuzzy-resolves a spoken/mis-transcribed symbol against currently pending proposals and echoes back exactly what it matched, or returns an explicit ambiguity or no-match result rather than guessing
- `M8-06` snooze_proposal and tag_proposal exist as safe-write MCP tools that annotate a pending proposal without touching its price, quantity, or approval state, and cannot increase exposure
- `M8-07` arm_alert and disarm_alert are exposed as MCP write tools over the existing alerts.py store unchanged, usable mid-call to leave a level watched after the voice session ends
- `M8-08` halt is reachable and works from MCP, mechanically proven at runtime (not only by the AST pin) as the positive counterpart to M0-09's mechanism-only test; resume is not reachable from any MCP tool; and trading_mcp/server.py's instructions= string, which falsely claims no tool can touch the halt/circuit-breaker switches, is corrected in this same change so the server's self-description is never contradicted by its own tool set
- `M8-09` If a run_scan or run_backtest MCP tool exists it is provably confined to the analysis path and can never construct or return an order payload; if no such tool is registered, that absence is itself a valid, explicitly-asserted PASS -- fabricating a tool solely to satisfy this feature is forbidden
- `M8-10` get_account_state bounds its positions payload so a large account does not produce an unreadable response
- `M8-11` get_audit_trail defaults to a spoken-friendly size with a compact summary mode, while full detail stays available on explicit request
- `M8-12` Every voice-originated MCP tool call (watch_setup, find_pending_setup, snooze/tag_proposal, arm/disarm_alert, halt) is written to the audit trail with tool name, arguments, and timestamp, and never logs a credential
- `M8-13` No speech-to-text, audio endpoint, or voice-command path exists anywhere in the repo, and every module added in this milestone is covered by M0-09's exposure-boundary pin
- `M8-14` OAuth-granted tokens carry scopes mirroring the exposure rule's read/safe-write tiers, and enforcement is proven against M8's actual safe-write tools (arm_alert, disarm_alert, halt, snooze_proposal, tag_proposal) -- the scope-tiering-enforcement half split out of M2-05, moved here because it needs these tools to exist
- `M8-15` draft_proposal is an MCP tool that runs the SAME deterministic sizing and risk path the graph uses, registers a PENDING proposal, and returns its id — it can neither approve nor place
- `M8-16` A proposal drafted from the MCP reaches the configured approval channel as an interactive card and is resolvable by a button press, with the drafting recorded in the audit trail
- `M8-17` Drafting is protected against a mis-heard instruction: the resolved symbol and size are echoed back for confirmation, and repeated near-identical drafts are deduplicated rather than queued
- `M8-18` The rule-3 AST pin is extended to the drafting module and proves the exposure boundary still holds with a write path present

## M10 — Skills endpoint (0/7)

- `M10-01` Building on M8-08's one-line correction, trading_mcp/server.py's instructions= is rewritten into a short, always-injected block stating what the server is, the exposure rule verbatim, the two forbidden actions by name, and the watch cadence
- `M10-02` An @mcp.prompt copilot_setup(proposal_id) exists, scripting the setup-watching session: call watch_setup on a cadence, when to say nothing changed, and that voice can never press the Telegram button itself
- `M10-03` An @mcp.prompt morning_brief() composes account state, pending proposals, armed alerts, and the gamma flip into a short spoken-friendly opening statement, reusing M8's bounded tool outputs rather than a fresh unbounded fetch
- `M10-04` Every one of the 64 directories under skills/ is surfaced as a readable @mcp.resource at skill://<name>, discovered dynamically from skills/*/SKILL.md rather than hand-listed
- `M10-05` A curated skill://rules resource, hand-written rather than auto-scraped from CLAUDE.md, covers the operating rules a fresh voice client needs: gamma marks positioning not a forecast, the NVDA-to-'in video' mis-transcription example, the exposure rule, and 'buttons move money'
- `M10-06` The skill:// resource surface is read-only and path-safe: a crafted skill name cannot read a file outside skills/, and the new resource/prompt modules are covered by M0-09's exposure pin
- `M10-07` End to end: a client with zero local context runs one full co-pilot turn using only what the server provides -- instructions, copilot_setup(proposal_id), skill://rules, and watch_setup -- proven by an integration test chaining exactly those calls

## M7 — Deployment: two units, one checkout (0/9)

- `M7-01` The stale pre-migration deploy/ artefacts are retired rather than left to mislead
- `M7-02` Two separate env-contract example files replace the single .env.example, matching the post-M0 dependency split -- trading_mcp never needs Telegram, vesper never needs TRADING_AGENT_TOKEN or OAuth vars
- `M7-03` Three systemd unit files live in the repo, each pointing at the correct one of the two env contracts
- `M7-04` The Traefik dynamic config lives in the repo alongside the documented sudo step that installs it
- `M7-05` The DNS and hostname facts, including the wildcard footgun, are documented where a person debugging a 503 will find them
- `M7-06` A vesper-only redeploy never restarts trading-agent.service, and an MCP-only redeploy never restarts vesper-loop/vesper-listen -- proven live, not claimed
- `M7-07` A rollback procedure exists for a bad deploy of either deployable, exercised once on a scratch checkout rather than the live one
- `M7-08` The install script is rewritten for the two env contracts, stays idempotent, and never prints a credential value
- `M7-09` The rewritten install script is verified live on coolify: trading-agent.service is active; vesper-loop.service and vesper-listen.service are installed and enabled but explicitly STOPPED, since starting them with no credentials and Restart=on-failure would crash-loop on a shared box

## M3 — Credentials + live data on the box (0/7)

- `M3-01` The trimmed credential set is defined and written to .env.example
- `M3-02` Credentials reach coolify without any value appearing in the transcript
- `M3-03` The account tools return real data, verified by shape and never by value
- `M3-04` The TDPro-backed momentum tools return live data on coolify
- `M3-05` The EDGAR tools work on coolify
- `M3-06` Rate-limit discipline is preserved on the remote box
- `M3-07` A missing credential still degrades rather than crashing

## M4 — Full Vesper running remotely (0/10)

- `M4-01` The repo on coolify is updated to the current pushed HEAD
- `M4-02` The full test suite passes on the coolify box itself
- `M4-03` Memory headroom is measured before adding two more processes
- `M4-04` vesper-loop.service exists as a systemd user unit and starts cleanly
- `M4-05` vesper-listen.service exists as a systemd user unit and starts cleanly
- `M4-06` All three units survive a restart and come back automatically
- `M4-07` The two processes agree on state file locations and do not corrupt each other
- `M4-08` The alert watcher runs inside the loop process as a thread, not an asyncio task
- `M4-09` The circuit breaker and halt file are honoured by the remote loop
- `M4-10` chromadb is either installed with measured headroom or its absence is asserted to be quiet

## M5 — Discord approvals from the remote box (0/9)

- `M5-01` **[human]** A dedicated Vesper Discord bot exists with its own token, invited to a private approvals-only channel, and its ids recorded in .env.example
- `M5-02` The approval channel's user allowlist fails CLOSED when unset — an unset DISCORD_AUTHORIZED_USER_IDS denies every interaction
- `M5-03` Authorization is enforced on every interaction, not only the first, on whichever channel is configured
- `M5-04` A proposal generated on the deployment box reaches the Discord approvals channel as an interactive card
- `M5-05` A button press resolves the proposal, resumes the graph, and is recorded in the audit trail
- `M5-06` Button interactions survive a bot restart — the stateless custom_id pattern works in practice, not just in principle
- `M5-07` A pending approval survives a restart of the loop unit
- `M5-08` The approval path stays outbound-only — a gateway WebSocket, never an inbound listener
- `M5-09` Approvals can be granted by nothing except a button press on the configured channel

## M6 — Soak, then arming (0/7)

- `M6-01` A full paper-mode session runs on coolify without an unhandled exception
- `M6-02` Proposals, approvals and exits round-trip in paper mode
- `M6-03` The audit chain verifies after the soak
- `M6-04` Memory is stable across the soak
- `M6-05` The kill switch and halt both work from the remote box
- `M6-06` docs/ARMING.md exists and is precise enough to follow under pressure
- `M6-07` VESPER_TRADING is still 0 at the end of the run

## M9 — Docs tell the truth (0/6)

- `M9-01` CLAUDE.md rule 1 is rewritten to describe the real network posture
- `M9-02` CLAUDE.md rule 4d is replaced with the claude.ai voice design
- `M9-03` CLAUDE.md's Status, Layout and test count are accurate
- `M9-04` The superseded supermcp consolidation docs point at the current decision
- `M9-05` ROADMAP.md reflects the work done and the work deliberately not done
- `M9-06` No credential, balance, account number or ntfy topic exists anywhere in git history from this run

## H — Human-only (2/4)

- `H3` **[human]** BLOCKED (human decision): arm live trading
- `H4` **[human]** BLOCKED (verification only): confirm voice works end to end on the phone

## Order of execution

Dependency order, not numeric: **M2 → M8 → M10 → M7 → M3 → M4 → M5 → M6 → M9**. M0 landed first because everything layers on it.

## Yours

- **M5-01** create a dedicated Discord bot + private approvals-only channel. Do not reuse `disclaw` (no authorization on its approval flow) or TraderDiscord-v2 (someone else's production).
- **H3** arm live trading. Never an agent's keystroke.
- **H4** confirm voice works on the phone.
