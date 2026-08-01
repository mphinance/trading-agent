#!/usr/bin/env bash
# sidecar launcher.
#
# Binds to 127.0.0.1 by default. SIDECAR_HOST can widen that — but read this
# first: sidecar has NO AUTHENTICATION, holds live brokerage credentials, and
# CAN PLACE ORDERS. Binding it to 0.0.0.0 lets anyone on the network read the
# account's balances and positions *and trade with them*. The supported way to
# reach it from other machines is a Tailscale IP (100.x.y.z), which is
# device-authenticated and invisible to the LAN and the internet.
#
# SIDECAR_TRADING=0 turns the order path off if you need the deck somewhere
# less private. See "Trading" in the README for the rest of the guards.
set -euo pipefail
cd "$(dirname "$0")"

# Credentials load from files, never from the calling shell — an `export` in an
# interactive terminal does not reach this process.
#   .env.webull    WEBULL_KEY / WEBULL_SECRET (required), TD_API_KEY (optional)
#   .env.anthropic CLAUDE_CODE_OAUTH_TOKEN (subscription) or ANTHROPIC_API_KEY
#   .env.notify    NTFY_TOPIC (no signup) and/or TELEGRAM_BOT_TOKEN + CHAT_ID
for f in ../.env.webull ../.env.anthropic ../.env.notify ../.env.telegram; do
  [ -f "$f" ] && set -a && . "$f" && set +a
done

# The Python Agent SDK shells out to the `claude` binary; under systemd the
# PATH is minimal, so make sure node's bin dir is reachable.
for d in "$HOME/.nvm/versions/node"/*/bin "$HOME/.local/bin" /usr/local/bin; do
  [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH" ;; esac
done
export PATH

# Prefer the project venv when present. venus's was built on python3.10 back
# when the SDK pinned <3.14; SDK 2.0.16 declares '>=3.8,<3.15', so a fresh venv
# on the system python is fine now and the old one needs no rebuild.
UVICORN="uvicorn"
[ -x ./.venv/bin/uvicorn ] && UVICORN="./.venv/bin/uvicorn"

exec "$UVICORN" server:app \
  --host "${SIDECAR_HOST:-127.0.0.1}" \
  --port "${SIDECAR_PORT:-8787}" "$@"
