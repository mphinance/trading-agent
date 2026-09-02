# Autonomous coding harness — finishing Vesper

This wires Anthropic's [autonomous-coding quickstart][qs] to *this* repo so a
long-running loop can finish the trading agent and its MCP server: the full
Vesper agent deployed on the coolify box, an MCP connector that claude.ai web and
mobile can attach to, voice over that connector, and Telegram left as the only
way an order gets approved.

[qs]: https://github.com/anthropics/claude-quickstarts/tree/main/autonomous-coding

## Quick start

```bash
./autonomous/setup.sh        # clone upstream + install overrides
./autonomous/run.sh 1        # one round — watch what it does
./autonomous/run.sh 6        # then let it work
```

No credential to export. The Python SDK spawns the `claude` CLI and inherits the
environment, so a stored `claude` login is used automatically and the run bills
against the **subscription**, not the API.

`Ctrl+C` is always safe. Progress lives in git and `feature_list.json`; the next
run continues from there.

## What's here

| Path | What it is |
|---|---|
| `prompts/app_spec.txt` | The specification. Milestones M1–M9, ten invariants, the four settled decisions, the human-blocked items, and a checkable definition of done. |
| `prompts/initializer_prompt.md` | Session 1: read the governing docs, install the feature list, capture the baseline. No application code. |
| `prompts/coding_prompt.md` | Every later session: orient, regression-check, do one feature, verify for real, commit, push, write notes. |
| `feature_list.seed.json` | 75 features across 9 milestones. Session 1 copies this to `feature_list.json` verbatim. |
| `overrides/security.py` | The bash policy. Replaces upstream's npm-shaped allowlist. |
| `overrides/client.py` | SDK config. No Puppeteer, deploy allowed, protected files. |
| `overrides/test_security_policy.py` | 32 cases pinning the policy. `setup.sh` runs them. |
| `overrides/patch_upstream.py` | Idempotent edits to upstream's `agent.py` and entry point. |
| `setup.sh` / `run.sh` | Install and drive. |
| `harness/` | The upstream clone plus overrides. Gitignored, disposable. |

## The SDK

Upstream pins **`claude-code-sdk` 0.0.25**, which is a dead package. The
maintained successor is **`claude-agent-sdk`** (0.2.151, ~150 releases ahead), and
`setup.sh` installs that instead of upstream's `requirements.txt`.

This is not cosmetic. The Python SDK is a wrapper that spawns the `claude` CLI, and
a current CLI (2.1.x) emits a `rate_limit_event` message the old SDK cannot parse
— it raises `Unknown message type: rate_limit_event` and kills the session
mid-run. The migration is otherwise a clean swap: same `ClaudeSDKClient`, same
option fields, same methods, with `ClaudeCodeOptions` renamed to
`ClaudeAgentOptions`.

`overrides/patch_upstream.py` repoints `agent.py`'s import and replaces the entry
point's hardcoded `ANTHROPIC_API_KEY` gate. Both edits are idempotent and applied
by `setup.sh`, so upstream files stay close to upstream.

## How it differs from the upstream quickstart

Upstream builds a greenfield React app and verifies it by driving a browser. This
repo is a working Python system with 570 passing tests, live broker credentials,
and a deployed service on a box that also runs other people's production. So:

- **The feature list is pre-written, not generated.** Upstream's session 1 invents
  200 test cases from the spec. Here, session 1 copies `feature_list.seed.json` —
  written against a live audit of the repo and the deployment box. A regenerated
  list would lose that grounding.
- **No browser, no Puppeteer.** Verification is `pytest`, plus actually observing
  the remote service: `systemctl --user is-active`, `ss -ltnp`, a real HTTP call.
  A config file that says the right thing is not evidence.
- **The sandbox is off and ssh is on.** The run is authorised to deploy. The
  compensating control is `overrides/security.py`, which is much stricter than
  upstream's allowlist in every other direction.
- **The bash parser is quote-aware.** Upstream splits commands on `;` with a
  regex that ignores quoting, so an ordinary `python3 -c "import x; print(1)"`
  splits mid-string, fails to lex and is blocked as unparseable. The override
  replaces the lexer; upstream's three original validators are reused as-is.
- **The loop is capped by default.** Upstream's only exit condition is
  `--max-iterations`; with no cap it starts fresh sessions forever regardless of
  progress. `run.sh` defaults to 6 and `client.py` caps each session at 250 turns
  (`AUTONOMOUS_MAX_TURNS` to change it).

## The bash policy

`overrides/security.py` reuses upstream's command parser and replaces the policy.
Allowed: the Python toolchain, git, file inspection, `ssh`/`scp`/`curl`. Blocked,
mechanically rather than by asking nicely:

- **`ssh` reaches exactly one host** (`coolify`), and the remote command is
  inspected — no `sudo`, no `docker`, no system-wide `systemctl`, and
  `systemctl --user` only against `trading-agent*` / `vesper-*` units. Everything
  else on that box is somebody's production.
- **No credential literals in any command.** Anthropic keys, TraderDaddy keys,
  Telegram bot tokens, bearer tokens. This transcript gets read aloud on stream.
- **No `.env` reads that print values.** `cat .env` is blocked; `grep -c '^KEY='`
  and the name-listing `sed` idiom are allowed.
- **No `git push --force`, no `git clean`, no `filter-branch`.** There is
  uncommitted, unpushed in-flight work in this tree (`vesper/agents/`, the swarm
  and synthesis nodes) that `git clean -fdx` would destroy.
- **No command substitution** — `$(…)` and backticks hide commands from the
  validator.
- **`python -c` can't be an escape hatch** — blocked if it contains `os.system`,
  `subprocess`, `eval`, `exec` or `socket`.

`client.py` additionally makes `vesper/execution_guard.py` and `.env` read-only to
the agent. The guard is the only module that can move money, and no milestone
requires editing it.

Run the policy tests any time:

```bash
cd autonomous/harness && .venv/bin/python -m pytest -q ../overrides/test_security_policy.py
```

## Billing and cost control

**It runs on your subscription.** The SDK is a wrapper around the `claude` CLI, so
auth resolves the way Claude Code's does — and the practical consequence is one
trap worth knowing:

> **`ANTHROPIC_API_KEY` shadows the subscription.** If it is exported anywhere in
> the shell that starts the run, every session is billed per token against the
> API instead. `run.sh` warns loudly when it sees one; unset it to stay on the
> subscription.

So the budget being spent is **usage limits, not dollars** — the 5-hour and weekly
windows. Which is exactly why the model choice and the iteration cap matter:

- `./autonomous/run.sh 6` — six rounds. `run.sh 3 haiku` for cheap
  documentation or verification rounds; Sonnet for the real work.
- Each session is capped at 250 turns.
- Sessions are independent by design — fresh context each time, continuity via
  git, `feature_list.json` and `claude-progress.txt`. Stopping costs nothing.
- Start with `run.sh 1` after any change to the prompts. The first round tells you
  whether the spec is being read the way you intended, for the price of one
  session.

## What it cannot do

Four items need a human. They are `blocked: true` in the feature list, they never
flip to passing, and every session is told to re-surface them in
`claude-progress.txt`:

- **H1 — install the Traefik dynamic config.** Needs `sudo`. Until it runs there
  is no TLS and no connector. As of this writing the box answers on 503 by IP and
  fails the TLS handshake by name, so this is still outstanding.
- **H2 — add the connector in claude.ai.** Needs a browser login.
- **H3 — arm live trading.** `VESPER_TRADING=1` is your keystroke, never the
  agent's. It writes `docs/ARMING.md`; you run it.
- **H4 — confirm voice works on the phone.** Only you can hold the phone.

## What to watch

The loop prints every tool call. The things worth catching early:

- **`[BLOCKED]` lines.** One or two are the policy working. A run full of them
  means the spec is asking for something the policy forbids — read the reason.
- **A dropping test count.** Step 3 of every session is a regression check against
  the number in `claude-progress.txt`. If it drops, the session is supposed to
  stop and fix it rather than build.
- **Features flipping to `passes: true` in bulk.** They should land one per
  session, each with a commit. A burst means something is being marked without
  being verified — the most damaging failure available here, because every later
  session trusts that file.
- **`claude-progress.txt`.** It's the handoff between sessions and the fastest
  read on whether the run still understands what it's doing.

## Refreshing upstream

```bash
rm -rf autonomous/harness && ./autonomous/setup.sh
```

`setup.sh` re-clones and re-applies the overrides. Only `security.py`, `client.py`
and the three prompt files are replaced; everything else stays upstream.
