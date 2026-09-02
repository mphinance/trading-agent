#!/usr/bin/env bash
#
# Run the autonomous coding loop against this repo.
#
#   ./autonomous/run.sh [iterations] [model]
#
#   ./autonomous/run.sh            # 6 rounds on Sonnet (the default)
#   ./autonomous/run.sh 1          # one round, to watch what it does
#   ./autonomous/run.sh 3 haiku    # three cheap rounds
#   ./autonomous/run.sh 12         # a longer unattended stretch
#
# The iteration cap is deliberate. Upstream's loop has exactly one exit condition
# — max_iterations — and no check for "all features passing", so an uncapped run
# keeps starting fresh sessions forever. Always pass a number you are willing to
# pay for. Ctrl+C is safe: state lives in git and feature_list.json, and the next
# run picks up where this one stopped.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
HARNESS="$HERE/harness"

ITERATIONS="${1:-6}"

case "${2:-sonnet}" in
  sonnet) MODEL="claude-sonnet-5" ;;
  haiku)  MODEL="claude-haiku-4-5-20251001" ;;
  opus)   MODEL="claude-opus-5" ;;
  *)      MODEL="${2}" ;;
esac

[ -d "$HARNESS" ] || { echo "Run ./autonomous/setup.sh first."; exit 1; }

# Auth resolution is the claude CLI's: ANTHROPIC_API_KEY, then
# CLAUDE_CODE_OAUTH_TOKEN, then the stored login. An empty environment is the
# normal case and means subscription billing.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "!! ANTHROPIC_API_KEY is set — this run will be billed per token against"
  echo "   the API, not your subscription. Unset it to use the subscription."
  echo
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "Auth:       CLAUDE_CODE_OAUTH_TOKEN (subscription)"
elif [ -f "$HOME/.claude/.credentials.json" ]; then
  echo "Auth:       stored claude login (subscription)"
else
  echo "No Claude credential found."
  echo "  Run 'claude' once and log in, or export CLAUDE_CODE_OAUTH_TOKEN,"
  echo "  or export ANTHROPIC_API_KEY to bill the API instead."
  exit 1
fi

command -v claude >/dev/null || {
  echo "The 'claude' CLI is not on PATH. The SDK spawns it, so it is required:"
  echo "  npm install -g @anthropic-ai/claude-code"
  exit 1
}

if ! ssh -o BatchMode=yes -o ConnectTimeout=8 coolify true 2>/dev/null; then
  echo "WARNING: 'ssh coolify' is not working unattended."
  echo "         Milestones M3 onward need it. Continuing anyway."
  echo
fi

echo "Repo:       $REPO"
echo "Model:      $MODEL"
echo "Iterations: $ITERATIONS"
echo
if [ -f "$REPO/feature_list.json" ]; then
  total=$(grep -c '"id"' "$REPO/feature_list.json" || true)
  done_n=$(grep -c '"passes": true' "$REPO/feature_list.json" || true)
  echo "Progress:   ${done_n:-0} / ${total:-0} features passing"
  echo
fi

cd "$HARNESS"
exec "$HARNESS/.venv/bin/python" autonomous_agent_demo.py \
  --project-dir "$REPO" \
  --model "$MODEL" \
  --max-iterations "$ITERATIONS"
