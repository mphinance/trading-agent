"""Refuse to serve the network with a secret nobody actually chose.

**The incident this exists to prevent (2026-09-03).** `trading-agent.service`
ran for a day on the internet-facing `agent.mphinance.com` with
`TRADING_AGENT_TOKEN=your_strong_random_secret_token` — the literal placeholder
copied out of `.env.trading-agent.example`, which is committed to a **public**
GitHub repo. Anyone who read the repo held a working bearer token for a server
carrying live Webull production credentials. Nothing detected it: the deploy
script left the value alone, the server started happily, the test suite was
green, and CI's credential scan looks for real-looking keys being committed —
the exact inverse of this failure, which is a fake-looking key being *deployed*.

The rule that follows, and the reason this is a module rather than a sentence in
a README: **a placeholder must not be able to become a live credential.** The
project already knows this shape of fix — `deploy/install.sh` refuses to run
without a Tailscale IP rather than falling back, and `server.py` refuses to open
an http listener with no token at all. This closes the remaining gap between
"no token" (already refused) and "a real token" (fine): the middle case of a
token-shaped string that was never generated.

Two deliberate choices worth not undoing:

- **`==` and `in`, not `hmac.compare_digest`.** Everywhere else in this package
  a token comparison is constant-time, because it runs against an attacker's
  submitted token on a live request path. This one runs once, at startup,
  against a local env var, with no remote party to time it. Constant-time
  comparison here would only obscure that difference.
- **A rejection reason never quotes the token.** Rule 5 — this work gets
  streamed, and startup logs are the most-screenshotted thing in the repo. The
  reason says what is wrong with the value, never what the value is. The same
  discipline `notify.status()` uses to keep the ntfy topic out of its output.
"""

from __future__ import annotations

# Exact placeholder values shipped in this repo's committed `.env.*.example`
# files. `tests/test_secret_hygiene.py::test_every_committed_example_placeholder_is_listed`
# re-derives this set from the example files themselves and fails if they drift
# apart, so editing an example's placeholder without updating this constant is
# a red build rather than a silent hole reopening.
PLACEHOLDER_TOKENS: frozenset[str] = frozenset(
    {
        "your_strong_random_secret_token",
    }
)

# Substrings that mean the value was typed by a human filling in a form rather
# than produced by a random-byte generator. Matched case-insensitively against
# the whole token.
WEAK_SUBSTRINGS: tuple[str, ...] = (
    "changeme",
    "change_me",
    "example",
    "placeholder",
    "password",
    "replace",
    "secret_token",
    "todo",
    "xxxx",
    "your_",
)

# `openssl rand -hex 32` — the command the docs and installer hand out — yields
# 64 characters. 32 is the floor, not the target: it admits a 16-byte hex token
# or a urlsafe-base64 24-byte one, and rejects anything a person would plausibly
# invent at a keyboard.
MIN_TOKEN_LENGTH = 32

# A 64-character token of "abababab..." clears the length bar and is worthless.
# Real hex has 16 distinct characters, base64url far more; a hand-made string
# ("mysupersecrettokenmysupersecrettoken") lands well under this.
MIN_DISTINCT_CHARACTERS = 10


# The example approver IDs from `.env.vesper.example`'s
# `TELEGRAM_AUTHORIZED_USER_IDS`. This one is nastier than the bearer-token
# placeholder, because copying it does not merely weaken a gate — it builds a
# *working* allowlist of two Telegram accounts that are not the operator. The
# owner is locked out of approving their own trades, no "unset" warning fires
# (the set is non-empty, so the allow-with-a-warning branch never runs), and
# whoever holds those account IDs is the only party who can tap approve.
# Treated as fail-closed by `core/approval_registry.py`: while a placeholder is
# in the allowlist, nobody is authorised, because refusing every approval
# cannot move money and trusting the wrong one can.
PLACEHOLDER_APPROVER_IDS: frozenset[str] = frozenset({"12345678", "87654321"})


def placeholder_approver_ids(configured_ids: set[str]) -> set[str]:
    """Return the configured approver IDs that are committed example values."""
    return {i for i in configured_ids if i in PLACEHOLDER_APPROVER_IDS}


def weak_token_reason(token: str | None) -> str | None:
    """Return why `token` is unfit to gate a network listener, or None if it's fine.

    The return value is a human-readable reason safe to log verbatim: it
    describes the defect and never reproduces the token.
    """
    if token is None or token == "":
        return "it is empty"

    if token in PLACEHOLDER_TOKENS:
        return (
            "it is the literal placeholder from a committed .env.*.example file, "
            "which means it is published in this repo and is not a secret at all"
        )

    lowered = token.lower()
    for marker in WEAK_SUBSTRINGS:
        if marker in lowered:
            return (
                f"it contains {marker!r}, which means it was filled in by hand "
                "rather than generated"
            )

    if len(token) < MIN_TOKEN_LENGTH:
        return (
            f"it is {len(token)} characters, below the {MIN_TOKEN_LENGTH}-character "
            "minimum for a token that gates a network listener"
        )

    if len(set(token)) < MIN_DISTINCT_CHARACTERS:
        return (
            f"it uses only {len(set(token))} distinct characters, below the "
            f"{MIN_DISTINCT_CHARACTERS} expected of a randomly generated token"
        )

    return None
