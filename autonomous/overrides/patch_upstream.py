"""
Targeted patches to upstream quickstart files.

`autonomous_agent_demo.py` gates on ANTHROPIC_API_KEY before it reaches
`client.py`, so overriding client.py alone is not enough — the run is rejected
with "Error: ANTHROPIC_API_KEY environment variable not set" even when a
subscription login is available.

Also repoints `agent.py` at the maintained SDK: upstream imports
`claude_code_sdk` 0.0.25, which cannot parse the `rate_limit_event` message a
current `claude` CLI emits and dies mid-session with "Unknown message type".

These are small, idempotent edits rather than whole-file overrides, so the
upstream files stay close to upstream.
"""

import sys
from pathlib import Path

OLD = '''    # Check for API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set")
        print("\\nGet your API key from: https://console.anthropic.com/")
        print("\\nThen set it:")
        print("  export ANTHROPIC_API_KEY='your-api-key-here'")
        return
'''

NEW = '''    # Credential check delegated to client.py, which resolves the way the claude
    # CLI does: ANTHROPIC_API_KEY, then CLAUDE_CODE_OAUTH_TOKEN, then the stored
    # subscription login. An empty environment is the normal case here, not an
    # error — the SDK spawns the CLI, which uses that stored login.
    from client import _check_credentials

    try:
        _check_credentials()
    except ValueError as exc:
        print(f"Error: {exc}")
        return
'''

DOC_OLD = "  ANTHROPIC_API_KEY    Your Anthropic API key (required)"
DOC_NEW = (
    "  (none required)      Uses your stored `claude` login by default.\n"
    "                       CLAUDE_CODE_OAUTH_TOKEN also works. ANTHROPIC_API_KEY\n"
    "                       overrides both and bills the API instead."
)


SDK_OLD = "from claude_code_sdk import ClaudeSDKClient"
SDK_NEW = "from claude_agent_sdk import ClaudeSDKClient"


def patch_entrypoint(target: Path) -> int:
    source = target.read_text()
    if NEW in source:
        print("   entry point already patched")
        return 0
    if OLD not in source:
        print(f"!! Could not find the credential gate in {target}.", file=sys.stderr)
        print("   Upstream may have changed it; patch it by hand.", file=sys.stderr)
        return 1
    target.write_text(source.replace(OLD, NEW, 1).replace(DOC_OLD, DOC_NEW, 1))
    print("   entry point credential gate patched")
    return 0


def patch_sdk_import(target: Path) -> int:
    source = target.read_text()
    if SDK_NEW in source:
        print("   agent.py already on claude-agent-sdk")
        return 0
    if SDK_OLD not in source:
        print(f"!! Could not find the SDK import in {target}.", file=sys.stderr)
        return 1
    target.write_text(source.replace(SDK_OLD, SDK_NEW, 1))
    print("   agent.py repointed to claude-agent-sdk")
    return 0


def main() -> int:
    harness = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    if harness.is_file():
        harness = harness.parent
    rc = patch_entrypoint(harness / "autonomous_agent_demo.py")
    return rc or patch_sdk_import(harness / "agent.py")


if __name__ == "__main__":
    raise SystemExit(main())
