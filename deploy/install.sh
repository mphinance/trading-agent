#!/usr/bin/env bash
# ==============================================================================
# Vesper & Trading-Agent Idempotent Deployment Script (Milestone 7)
# ==============================================================================
# Run ON target host: ~/trading-agent/deploy/install.sh
#
# Idempotent: Can be run repeatedly without causing duplicate state or unintended restarts.
# Security: Never logs or echoes credential values. Validates env files by name only.
# Safety: Starts ONLY trading-agent.service. Enables vesper-*.service but keeps them STOPPED.
# ==============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

echo "=== [1/5] Checking Python & Virtualenv ==="
if [ ! -d "$REPO_DIR/.venv" ]; then
    echo "Creating virtual environment at $REPO_DIR/.venv..."
    python3 -m venv "$REPO_DIR/.venv"
fi
source "$REPO_DIR/.venv/bin/activate"

echo "=== [2/5] Validating Environment Contracts (names only) ==="
ENV_TRADING="$REPO_DIR/.env.trading-agent"
ENV_VESPER="$REPO_DIR/.env.vesper"

if [ ! -f "$ENV_TRADING" ]; then
    echo "Notice: $ENV_TRADING not found. Creating from example template..."
    cp "$REPO_DIR/.env.trading-agent.example" "$ENV_TRADING"
    chmod 600 "$ENV_TRADING"
fi

if [ ! -f "$ENV_VESPER" ]; then
    echo "Notice: $ENV_VESPER not found. Creating from example template..."
    cp "$REPO_DIR/.env.vesper.example" "$ENV_VESPER"
    chmod 600 "$ENV_VESPER"
fi

# Print variable names present, NEVER values
echo "Variables defined in .env.trading-agent:"
grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_TRADING" | cut -d= -f1 | sed 's/^/  - /' || true

echo "Variables defined in .env.vesper:"
grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_VESPER" | cut -d= -f1 | sed 's/^/  - /' || true

echo "=== [3/5] Installing Systemd User Units ==="
mkdir -p "$UNIT_DIR"
for unit in trading-agent.service vesper-loop.service vesper-listen.service; do
    if [ -f "$REPO_DIR/deploy/$unit" ]; then
        install -m 644 "$REPO_DIR/deploy/$unit" "$UNIT_DIR/$unit"
        echo "Installed $unit to $UNIT_DIR"
    fi
done

echo "=== [4/5] Configuring User Lingering ==="
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

systemctl --user daemon-reload

echo "=== [5/5] Managing Service States ==="
# Enable all three units for start-on-boot
systemctl --user enable trading-agent.service
systemctl --user enable vesper-loop.service
systemctl --user enable vesper-listen.service

# Start ONLY trading-agent.service (restart if active, start if inactive)
if systemctl --user is-active --quiet trading-agent.service; then
    echo "Restarting active trading-agent.service..."
    systemctl --user restart trading-agent.service
else
    echo "Starting trading-agent.service..."
    systemctl --user start trading-agent.service
fi

# Ensure vesper services are explicitly STOPPED
# (prevents crash-loops when credentials / live-arming are not yet configured)
systemctl --user stop vesper-loop.service 2>/dev/null || true
systemctl --user stop vesper-listen.service 2>/dev/null || true

echo "=== Status Summary ==="
echo -n "trading-agent.service: "
systemctl --user is-active trading-agent.service || true

echo -n "vesper-loop.service: "
systemctl --user is-active vesper-loop.service || echo "inactive (expected)"

echo -n "vesper-listen.service: "
systemctl --user is-active vesper-listen.service || echo "inactive (expected)"

echo "Deployment script completed successfully."
