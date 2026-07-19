#!/usr/bin/env bash
# Install sidecar as a user systemd service bound to this machine's Tailscale IP.
#
# Run ON the target host:  ~/webull/sidecar/deploy/install.sh
#
# Binds to the tailnet address, never 0.0.0.0 — sidecar has no authentication
# and holds live brokerage credentials, so only your own Tailscale devices
# should be able to read the account through it.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [ -z "$TS_IP" ]; then
  echo "ERROR: no Tailscale IPv4 address on this machine." >&2
  echo "       Run 'sudo tailscale up' and authenticate, then re-run this script." >&2
  echo "       (Refusing to fall back to 0.0.0.0 — this app holds live credentials.)" >&2
  exit 1
fi

cat > "$HERE/sidecar.env" <<EOF
# Written by install.sh — this machine's Tailscale IP.
# Do NOT change to 0.0.0.0: no auth, holds live brokerage credentials.
SIDECAR_HOST=$TS_IP
SIDECAR_PORT=8787
EOF
chmod 600 "$HERE/sidecar.env"

mkdir -p "$UNIT_DIR"
install -m 644 "$HERE/sidecar.service" "$UNIT_DIR/sidecar.service"
chmod +x "$HERE/../run.sh"

# Survive logout / start at boot without a login session.
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

systemctl --user daemon-reload
systemctl --user enable --now sidecar.service

sleep 3
echo "--- status:"
systemctl --user --no-pager --lines=0 status sidecar.service | head -6 || true
echo
echo "sidecar bound to http://$TS_IP:8787"
echo "Reachable from any device on your tailnet. Not on the LAN, not on the internet."
