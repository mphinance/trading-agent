#!/usr/bin/env bash
# The one credential-pattern list. CI and the pre-commit hook both call this, so
# they cannot drift — which is exactly how the last one got through.
#
# 2026-09-03: a full `read,trade`-scoped supermcp key sat in
# docs/HANDOFF_2026-09-01.md and CI's scan walked straight past it, because the
# scan knew `sk-ant-`, `td_live_` and Telegram bot tokens and nothing about
# `smk_`. A scanner that only knows the prefixes it was born with goes stale in
# silence. **When a new credential format appears anywhere in the estate, add it
# here** — this file is the maintenance point, and tests/test_secret_hygiene.py
# pins that the known prefixes stay in it.
#
# Usage:
#   scripts/scan_secrets.sh            scan the working tree (CI)
#   scripts/scan_secrets.sh --staged   scan staged content only (pre-commit)
#
# Exit 0 = clean, 1 = something credential-shaped found.
#
# Every pattern demands enough entropy to be a real key, so the placeholders
# these docs are full of (`sk-ant-...`, `your_strong_random_secret_token`,
# `123456:AA...`) do not trip it. That matters: a scanner people learn to
# override is worse than no scanner.

set -uo pipefail

PATTERNS=(
    'sk-ant-[A-Za-z0-9_-]{20,}'          # Anthropic API / OAuth token
    'td_live_[A-Za-z0-9_-]{20,}'         # TraderDaddy / TMpro customer API key
    'smk_[A-Za-z0-9_-]{20,}'             # supermcp minted key
    '[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}'   # Telegram bot token
)

GREP_ARGS=()
for p in "${PATTERNS[@]}"; do
    GREP_ARGS+=(-e "$p")
done

if [ "${1:-}" = "--staged" ]; then
    # Only what is actually about to be committed. Added lines only: a pattern
    # already in history is a separate (worse) problem, and failing every commit
    # over it would just teach --no-verify.
    found=$(git diff --cached -U0 --no-color \
              | grep '^+' | grep -v '^+++' \
              | grep -InE "${GREP_ARGS[@]}" || true)
else
    # TRACKED FILES ONLY, via git ls-files — not a recursive grep of the
    # working tree. Rule 2 puts real credentials in gitignored env files by
    # design, so a tree walk flags `.env`, `.env.bak-*` and agent logs every
    # single run: all correct, all noise, and noise is how a scanner gets
    # ignored. CI only ever sees tracked files anyway, so this makes local and
    # CI answer the same question.
    found=$(git ls-files -z \
              | grep -zv '^scripts/scan_secrets\.sh$' \
              | xargs -0 grep -d skip -InE "${GREP_ARGS[@]}" /dev/null || true)
fi

if [ -n "$found" ]; then
    echo "credential-shaped string detected:" >&2
    # Print WHERE, never the matched value — this output lands in CI logs and
    # terminal scrollback, and this work gets streamed (rule 5).
    echo "$found" | sed -E 's/:.*/: <redacted match>/' >&2
    echo "" >&2
    echo "If it is a real credential: do not commit it. Rotate it if it ever left" >&2
    echo "this machine, and read the value from the service's own .env instead." >&2
    echo "If it is genuinely a placeholder, make it lower-entropy so it reads as one." >&2
    exit 1
fi

echo "credential scan: clean"
exit 0
