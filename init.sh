#!/usr/bin/env bash
# init.sh — idempotent environment bootstrap for a fresh autonomous session.
#
# What this does:
#   - creates/reuses .venv
#   - installs requirements-dev.txt (NOT requirements.txt — see CLAUDE.md:
#     the Webull SDK and Agent SDK are stubbed in tests/conftest.py on purpose)
#   - prints (does not run) the commands that verify local + remote state
#
# What this deliberately does NOT do:
#   - start vesper.py loop / listen / any long-running process
#   - touch the coolify box
#   - print any credential, account number, balance or ntfy topic
#
# Safe to re-run any time.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d .venv ]; then
  echo "[init.sh] creating .venv"
  python3 -m venv .venv
fi

echo "[init.sh] installing requirements-dev.txt into .venv"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements-dev.txt

echo
echo "[init.sh] environment ready. Verification commands (run them yourself, not part of init):"
cat <<'EOF'

# 1. Local test suite — floor is 570 passing as of 2026-09-01 (see claude-progress.txt)
.venv/bin/python -m pytest -q

# 2. Confirm the package still imports (the M1 problem lives here)
.venv/bin/python -c "import vesper.nodes; print('vesper.nodes OK')"

# 3. Clean-venv dependency check (catches missing transitive deps that no test imports directly)
#    Run this in a SEPARATE throwaway venv, not the one above:
#      python3 -m venv /tmp/clean-venv && /tmp/clean-venv/bin/pip install -r requirements-dev.txt \
#        && /tmp/clean-venv/bin/python -m pytest --collect-only

# 4. Remote service state (names only — never print a value, rule 5 / invariant I6)
ssh coolify 'systemctl --user is-active trading-agent.service; ss -ltnp | grep 8500'
ssh coolify 'sed -n "s/^\([A-Z_]*\)=.*/\1/p" ~/trading-agent/.env'

# 5. Is TLS live yet? (expect failure/4xx until H1 — the sudo-gated Traefik install — runs)
curl -s -o /dev/null -w "by-name: %{http_code}\n" https://agent.mphinance.com/mcp

EOF
