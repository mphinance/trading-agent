"""Pins for the 2026-09-03 placeholder-in-production incident.

`trading-agent.service` served the public internet for a day with
`TRADING_AGENT_TOKEN` set to the literal placeholder committed in
`.env.trading-agent.example`. These tests exist so that specific failure cannot
come back quietly: they pin the guard itself, the fact that `server.py` actually
calls it before opening a listener, the fail-closed behaviour of a placeholder
approver allowlist, and — the one most likely to rot — that the guard's list of
placeholders still matches what the example files actually ship.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.secret_hygiene import (
    MIN_DISTINCT_CHARACTERS,
    MIN_TOKEN_LENGTH,
    PLACEHOLDER_APPROVER_IDS,
    PLACEHOLDER_TOKENS,
    placeholder_approver_ids,
    weak_token_reason,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# A real one, so the "good token" case is not accidentally testing a weak value.
GOOD_TOKEN = "9f2c1a7be40d83516ec4b0d92af715c38ba6e0d47c1985fe23b0ac6d18e4f70b"


def test_the_exact_incident_value_is_rejected():
    reason = weak_token_reason("your_strong_random_secret_token")
    assert reason is not None
    assert "placeholder" in reason


@pytest.mark.parametrize("token", sorted(PLACEHOLDER_TOKENS))
def test_every_known_placeholder_is_rejected(token):
    assert weak_token_reason(token) is not None


def test_a_real_random_token_is_accepted():
    assert weak_token_reason(GOOD_TOKEN) is None


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "changeme",
        "CHANGEME_please_this_is_long_enough_to_pass_length",
        "my_example_token_that_is_long_enough_to_pass",
        "your_secret_here_padded_out_to_be_long_enough",
        "replace-this-value-with-something-random-later",
        "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "short",
        "a" * 64,  # long, but one distinct character
        "abababababababababababababababababababababab",
    ],
)
def test_unfit_tokens_are_rejected(token):
    assert weak_token_reason(token) is not None


def test_length_and_distinctness_thresholds_are_the_boundary():
    just_short = GOOD_TOKEN[: MIN_TOKEN_LENGTH - 1]
    assert weak_token_reason(just_short) is not None
    assert weak_token_reason(GOOD_TOKEN[:MIN_TOKEN_LENGTH]) is None
    assert len(set(GOOD_TOKEN[:MIN_TOKEN_LENGTH])) >= MIN_DISTINCT_CHARACTERS


def test_a_rejection_reason_never_quotes_the_token():
    """Rule 5: startup logs get streamed and screenshotted.

    The reason is logged verbatim by server.py, so it must describe the defect
    without reproducing the value — the same discipline that keeps the ntfy
    topic out of `notify.status()`.
    """
    secretish = "changeme_" + GOOD_TOKEN
    reason = weak_token_reason(secretish)
    assert reason is not None
    assert secretish not in reason
    assert GOOD_TOKEN not in reason


def _example_files() -> list[Path]:
    return sorted(REPO_ROOT.glob(".env*.example"))


def test_every_committed_example_placeholder_is_listed():
    """The guard's denylist must track what the examples actually ship.

    Editing `.env.trading-agent.example`'s placeholder without updating
    `PLACEHOLDER_TOKENS` would silently reopen the exact hole this module
    closes, so that drift is a failing test rather than a quiet regression.
    """
    assert _example_files(), "no .env*.example files found — did they move?"
    for example in _example_files():
        for line in example.read_text().splitlines():
            match = re.match(r"^TRADING_AGENT_TOKEN=(.+)$", line.strip())
            if match:
                value = match.group(1).strip()
                assert value in PLACEHOLDER_TOKENS, (
                    f"{example.name} ships TRADING_AGENT_TOKEN={value!r}, which is not in "
                    "core.secret_hygiene.PLACEHOLDER_TOKENS. Add it there, or the server "
                    "will happily boot with this exact value in production."
                )


def test_no_example_file_ships_an_uncommented_approver_allowlist():
    """An uncommented approver allowlist in an example is a live foot-gun.

    Copied verbatim it produces a *working* allowlist of accounts that are not
    the operator: no "unset" warning fires, the owner cannot approve their own
    trades, and the published IDs are the only ones that can.
    """
    for example in _example_files():
        for lineno, line in enumerate(example.read_text().splitlines(), start=1):
            assert not line.strip().startswith("TELEGRAM_AUTHORIZED_USER_IDS="), (
                f"{example.name}:{lineno} ships an uncommented "
                "TELEGRAM_AUTHORIZED_USER_IDS — keep it commented out."
            )


def test_placeholder_approver_ids_detects_the_committed_examples():
    configured = {"12345678", "555000111"}
    assert placeholder_approver_ids(configured) == {"12345678"}
    assert placeholder_approver_ids({"555000111"}) == set()
    assert PLACEHOLDER_APPROVER_IDS


def test_approval_registry_fails_closed_on_a_placeholder_allowlist(monkeypatch):
    """A placeholder allowlist authorises NOBODY.

    Refusing every approval cannot move money; trusting an allowlist of
    published example IDs can. This is the one place the project's usual
    "single operator, allow-with-a-warning" default must invert.
    """
    from core import approval_registry

    monkeypatch.setattr(
        approval_registry, "_AUTHORIZED_TELEGRAM_USER_IDS", {"12345678", "87654321"}
    )
    monkeypatch.setattr(approval_registry, "_warned_telegram_placeholder", False)

    assert approval_registry._is_telegram_user_authorized("12345678") is False
    assert approval_registry._is_telegram_user_authorized("999999999") is False


def test_approval_registry_still_authorises_a_real_allowlist(monkeypatch):
    from core import approval_registry

    monkeypatch.setattr(approval_registry, "_AUTHORIZED_TELEGRAM_USER_IDS", {"707070707"})
    assert approval_registry._is_telegram_user_authorized("707070707") is True
    assert approval_registry._is_telegram_user_authorized("12345678") is False


def test_server_http_path_calls_the_guard_before_listening():
    """AST pin: the http branch must consult `weak_token_reason` and exit.

    A presence-only check ("is the token set?") is what shipped the incident,
    and it is the easy thing to fall back to while editing this block. Matching
    the call by AST means deleting or bypassing it fails a test rather than a
    code review, the same way test_stream_runner.py pins the monitor's push
    wake-up.
    """
    source = (REPO_ROOT / "trading_mcp" / "server.py").read_text()
    tree = ast.parse(source)

    http_branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(cmp_node, ast.Constant) and cmp_node.value == "http"
            for cmp_node in ast.walk(node.test)
        )
    ]
    assert http_branches, "could not find the `transport == 'http'` branch in server.py"

    calls_guard = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "weak_token_reason"
        for branch in http_branches
        for node in ast.walk(branch)
    )
    assert calls_guard, (
        "trading_mcp/server.py's http transport branch no longer calls "
        "weak_token_reason(). A presence-only token check is exactly what "
        "allowed the 2026-09-03 placeholder to serve production traffic."
    )

    exits = any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "SystemExit"
        for branch in http_branches
        for node in ast.walk(branch)
    )
    assert exits, "the http branch must refuse to start, not just warn"
