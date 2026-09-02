"""M0-04: ApprovalRegistry moved from vesper/bot/inbound.py into
core/approval_registry.py so that reading pending approvals no longer has
to import the `vesper.bot` package -- whose `__init__.py` eagerly
constructs TelegramAdapter/DiscordAdapter/WebhookAdapter/ChannelManager,
none of which a read-only caller (trading_mcp/vesper_tools.py's
`list_pending_proposals` / `get_proposal` tools, in particular) has any
business instantiating.

This is checked as a real subprocess import, not just "the source doesn't
say `import vesper.bot`" -- a transitive import elsewhere in the chain
could still pull the adapters in, and the whole point of the move is the
runtime behaviour (what actually lands in `sys.modules`), not the text of
one file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_ADAPTER_MODULES = (
    "vesper.bot.telegram_adapter",
    "vesper.bot.discord_adapter",
    "vesper.bot.webhook_adapter",
)


def test_importing_core_approval_registry_does_not_load_bot_adapters():
    """A fresh interpreter that imports only `core.approval_registry` must
    not end up with any of the channel adapter modules in `sys.modules` --
    those only get pulled in via `vesper/bot/__init__.py`, and
    core/approval_registry.py has no dependency on `vesper.bot` at all."""
    probe = (
        "import sys\n"
        "import core.approval_registry\n"
        "loaded = [m for m in %r if m in sys.modules]\n"
        "assert not loaded, f'unexpectedly loaded: {loaded}'\n"
        "assert 'vesper.bot' not in sys.modules, "
        "'importing core.approval_registry pulled in the vesper.bot package'\n"
        "print('OK')\n"
    ) % (list(_ADAPTER_MODULES),)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"probe failed (rc={result.returncode})\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stdout.strip() == "OK"


def test_vesper_bot_inbound_still_re_exports_approval_registry():
    """Backward compatibility: callers that still do
    `from vesper.bot.inbound import approval_registry` / `ApprovalRegistry`
    (vesper.py, vesper/runner.py, vesper/bot/discord_gateway.py,
    vesper/bot/telegram_polling.py, and several tests) must keep working --
    this feature only relocates *where* the class is defined, not every
    caller's import path. The two names must be the SAME objects as
    core.approval_registry's, not copies, so patching one location (tests,
    `set_graph_app`, etc.) is visible through both.
    """
    import core.approval_registry as core_mod
    import vesper.bot.inbound as inbound_mod

    assert inbound_mod.ApprovalRegistry is core_mod.ApprovalRegistry
    assert inbound_mod.approval_registry is core_mod.approval_registry
