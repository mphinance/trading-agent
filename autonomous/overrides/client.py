"""
Claude SDK client configuration — trading-agent override
========================================================

Drop-in replacement for the quickstart's `client.py`. Four differences:

0. **The current SDK.** Upstream pins `claude-code-sdk` 0.0.25, a dead package.
   The maintained successor is `claude-agent-sdk` (0.2.x, ~150 releases ahead).
   The old one cannot parse the `rate_limit_event` message that a current
   `claude` CLI emits, which kills a session mid-run. Same `ClaudeSDKClient`,
   same option fields, same methods — only `ClaudeCodeOptions` is renamed to
   `ClaudeAgentOptions`.
1. **No Puppeteer.** This project has no browser and no UI. Verification here is
   pytest plus real observation of a remote systemd service.
2. **Sandbox off.** The run is explicitly authorised to deploy over ssh, which the
   OS sandbox blocks. The compensating control is `security.py`, which is
   considerably stricter than the upstream allowlist: one permitted ssh host,
   inspected remote commands, no sudo, no docker, no force-push, no `git clean`,
   no credential literals, and no dotenv reads that would print values.
3. **It runs on the subscription, with no credential in the environment.** The
   Python SDK is a thin wrapper that spawns the `claude` CLI and inherits
   `os.environ` wholesale, so auth is exactly Claude Code's auth: a stored
   `claude` login is used automatically. `ANTHROPIC_API_KEY` **shadows** that and
   switches the whole run to metered API billing, so its presence is treated as a
   warning, not a requirement (CLAUDE.md rule 7).
4. **Money-moving and secret-bearing files are read-only to the agent.**
   `vesper/execution_guard.py` is the only module that can place an order, and
   nothing in this run's milestones requires editing it.
"""

import json
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import HookMatcher

from security import bash_security_hook


BUILTIN_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
]

# Files the agent may read but must never modify.
PROTECTED_PATHS = [
    "./vesper/execution_guard.py",   # the only module that can move money
    "./.env",                        # live broker credentials
    "./.env.*",
]

SYSTEM_PROMPT = (
    "You are a careful senior engineer working on Vesper, an autonomous trading "
    "agent that can place real orders against a live brokerage account, deployed "
    "on a shared server that also runs other people's production. "
    "Read CLAUDE.md and app_spec.txt before changing anything. Prefer finishing "
    "one feature with a passing test over starting three. Never mark work as "
    "verified that you did not actually run. Never print a credential, an account "
    "number or a balance — this transcript is read aloud on stream. When you are "
    "blocked by a missing privilege, record the blocker and move on; do not "
    "engineer around it."
)

# Bounded so a single runaway session cannot consume an evening's budget.
MAX_TURNS_PER_SESSION = int(os.environ.get("AUTONOMOUS_MAX_TURNS", "250"))


def _check_credentials() -> None:
    """
    Report how this run will be billed, and refuse only if there is no way in.

    Resolution order is the CLI's, not ours: ANTHROPIC_API_KEY wins if set, then
    CLAUDE_CODE_OAUTH_TOKEN, then the stored `claude` login. The common case here
    is the third — nothing exported, subscription billing — so an unset
    environment is normal and must not be an error.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("!! ANTHROPIC_API_KEY is set.")
        print("   It takes precedence over your Claude subscription, so this run")
        print("   will be billed per token against the API. Unset it to run on the")
        print("   subscription instead.")
        print()
        return

    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("   Auth: CLAUDE_CODE_OAUTH_TOKEN (subscription)")
        return

    if (Path.home() / ".claude" / ".credentials.json").exists():
        print("   Auth: stored `claude` login (subscription)")
        return

    raise ValueError(
        "No Claude credential found.\n"
        "Run `claude` once and log in (subscription — preferred, CLAUDE.md rule 7),\n"
        "or export CLAUDE_CODE_OAUTH_TOKEN, or export ANTHROPIC_API_KEY to bill the API."
    )


def create_client(project_dir: Path, model: str) -> ClaudeSDKClient:
    """
    Create a Claude Agent SDK client for one session.

    Security layers (defense in depth):
    1. Permissions — file writes confined to the project dir, minus PROTECTED_PATHS
    2. Security hook — every bash command validated against security.py's policy
    3. The prompts themselves — app_spec.txt §2 invariants, restated each session
    """
    _check_credentials()

    security_settings = {
        # Disabled deliberately: this run deploys over ssh. See module docstring.
        "sandbox": {"enabled": False},
        "permissions": {
            "defaultMode": "acceptEdits",
            "allow": [
                "Read(./**)",
                "Write(./**)",
                "Edit(./**)",
                "Glob(./**)",
                "Grep(./**)",
                # Granted here, but every command is vetted by bash_security_hook.
                "Bash(*)",
            ],
            "deny": [
                *[f"Write({p})" for p in PROTECTED_PATHS],
                *[f"Edit({p})" for p in PROTECTED_PATHS],
            ],
        },
    }

    project_dir.mkdir(parents=True, exist_ok=True)
    settings_file = project_dir / ".claude_settings.json"
    with open(settings_file, "w") as f:
        json.dump(security_settings, f, indent=2)

    print(f"Settings written to {settings_file}")
    print(f"   - Model: {model}")
    print(f"   - Max turns this session: {MAX_TURNS_PER_SESSION}")
    print(f"   - File writes confined to: {project_dir.resolve()}")
    print(f"   - Read-only to the agent: {', '.join(PROTECTED_PATHS)}")
    print("   - Bash policy: autonomous/overrides/security.py (ssh -> coolify only)")
    print()

    return ClaudeSDKClient(
        options=ClaudeAgentOptions(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            allowed_tools=BUILTIN_TOOLS,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="Bash", hooks=[bash_security_hook]),
                ],
            },
            max_turns=MAX_TURNS_PER_SESSION,
            cwd=str(project_dir.resolve()),
            settings=str(settings_file.resolve()),
        )
    )
