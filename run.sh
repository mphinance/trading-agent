#!/usr/bin/env bash
# sidecar launcher.
#
# Binds to 127.0.0.1 by default. SIDECAR_HOST can widen that — but read this
# first: sidecar has NO AUTHENTICATION and holds live brokerage credentials.
# It's read-only (it never places orders), but account balances and positions
# still shouldn't leak to the LAN. Binding it to 0.0.0.0 lets anyone on the
# network read the account. The supported way to reach it from other machines
# is a Tailscale IP (100.x.y.z), which is device-authenticated and invisible
# to the LAN and the internet.
set -euo pipefail
cd "$(dirname "$0")"

# Credentials load from files, never from the calling shell — an `export` in an
# interactive terminal does not reach this process.
#   .env.webull    WEBULL_KEY / WEBULL_SECRET (required), TD_API_KEY (optional)
#   .env.anthropic CLAUDE_CODE_OAUTH_TOKEN (subscription) or ANTHROPIC_API_KEY
for f in ../.env.webull ../.env.anthropic; do
  [ -f "$f" ] && set -a && . "$f" && set +a
done

# The Python Agent SDK shells out to the `claude` binary; under systemd the
# PATH is minimal, so make sure node's bin dir is reachable.
for d in "$HOME/.nvm/versions/node"/*/bin "$HOME/.local/bin" /usr/local/bin; do
  [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH" ;; esac
done
export PATH

# Prefer the project venv when present (venus builds one on python3.10, since
# the Webull SDK requires >=3.8,<3.14 and that box defaults to 3.14).
UVICORN="uvicorn"
[ -x ./.venv/bin/uvicorn ] && UVICORN="./.venv/bin/uvicorn"

exec "$UVICORN" server:app \
  --host "${SIDECAR_HOST:-127.0.0.1}" \
  --port "${SIDECAR_PORT:-8787}" "$@"
