#!/usr/bin/env bash
# MCP launcher — this is what Claude Desktop spawns.
#
# It speaks MCP on stdio and talks to a *running* sidecar over HTTP, so start
# ./run.sh first. Nothing here holds broker credentials: the sidecar owns the
# Webull client, its cache, and its rate-limit budget, and this process just
# forwards calls to it.
#
# Claude Desktop config (claude_desktop_config.json):
#
#   {
#     "mcpServers": {
#       "webull-sidecar": {
#         "command": "/home/YOU/webull-sidecar/mcp.sh"
#       }
#     }
#   }
#
# If sidecar runs on another box (e.g. venus over Tailscale), point this at it:
#   SIDECAR_URL=http://100.113.21.73:8787
set -euo pipefail
cd "$(dirname "$0")"

# Default to the local sidecar; override in the environment or the Desktop config.
export SIDECAR_URL="${SIDECAR_URL:-http://127.0.0.1:8787}"

# stdout is the MCP transport — anything else printed there corrupts the
# protocol, so keep diagnostics on stderr.
PY="python3"
[ -x ./.venv/bin/python ] && PY="./.venv/bin/python"

exec "$PY" mcp_server.py "$@"
