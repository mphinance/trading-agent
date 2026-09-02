#!/usr/bin/env bash
#
# Set up the autonomous-coding harness for this repo.
#
# Clones Anthropic's quickstart into autonomous/harness/, then lays this repo's
# overrides on top of it. The upstream clone stays pristine underneath, so it can
# be refreshed by deleting autonomous/harness/ and re-running this.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
HARNESS="$HERE/harness"
UPSTREAM="https://github.com/anthropics/claude-quickstarts.git"

command -v git >/dev/null || { echo "git is required"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

if [ -d "$HARNESS" ]; then
  echo "==> harness/ already exists; refreshing overrides only"
  echo "    (delete autonomous/harness to re-clone upstream)"
else
  echo "==> Cloning the quickstart"
  TMP="$(mktemp -d)"
  git clone --depth 1 --filter=blob:none --sparse "$UPSTREAM" "$TMP/qs"
  git -C "$TMP/qs" sparse-checkout set autonomous-coding
  mkdir -p "$HARNESS"
  cp -R "$TMP/qs/autonomous-coding/." "$HARNESS/"
  rm -rf "$TMP"

  echo "==> Preserving the upstream bash parser as security_upstream.py"
  cp "$HARNESS/security.py" "$HARNESS/security_upstream.py"
fi

echo "==> Installing overrides"
cp "$HERE/overrides/security.py" "$HARNESS/security.py"
cp "$HERE/overrides/client.py"   "$HARNESS/client.py"
cp "$HERE/prompts/app_spec.txt"           "$HARNESS/prompts/app_spec.txt"
cp "$HERE/prompts/initializer_prompt.md"  "$HARNESS/prompts/initializer_prompt.md"
cp "$HERE/prompts/coding_prompt.md"       "$HARNESS/prompts/coding_prompt.md"
python3 "$HERE/overrides/patch_upstream.py" "$HARNESS"

echo "==> Creating the harness venv"
if [ ! -d "$HARNESS/.venv" ]; then
  python3 -m venv "$HARNESS/.venv"
fi
"$HARNESS/.venv/bin/pip" install --quiet --upgrade pip
# NOT requirements.txt: upstream pins claude-code-sdk 0.0.25, a dead package that
# cannot parse a current CLI's rate_limit_event message.
"$HARNESS/.venv/bin/pip" install --quiet claude-agent-sdk pytest

echo "==> Verifying the security policy"
( cd "$HARNESS" && "$HARNESS/.venv/bin/python" -m pytest -q "$HERE/overrides/test_security_policy.py" )

echo "==> Keeping harness artefacts out of git"
for entry in \
  "autonomous/harness/" \
  ".claude_settings.json" \
  "Cash Secured Puts-*.csv" \
  "Screenshot *.png"
do
  grep -qxF "$entry" "$REPO/.gitignore" 2>/dev/null || echo "$entry" >> "$REPO/.gitignore"
done

cat <<'DONE'

Setup complete.

  Next:
    1. Nothing to export — a stored `claude` login is used automatically and
       the run bills against your subscription. If ANTHROPIC_API_KEY is
       exported in your shell it will shadow that and bill the API instead;
       run.sh warns you when it sees one.
    2. Confirm ssh works unattended:
         ssh -o BatchMode=yes coolify true && echo ok
    3. Start with a single session and read what it does:
         ./autonomous/run.sh 1

  autonomous/README.md explains the loop, the cost controls, and what to watch.
DONE
