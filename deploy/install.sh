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
    # Generate the one secret this script CAN generate, rather than shipping the
    # example's placeholder into production. On 2026-09-03 that placeholder ran
    # live on a public hostname for a day: the copy above happened, the human
    # step that was supposed to follow it did not, and nothing noticed. A value
    # that must be random is better produced here than remembered later.
    if command -v openssl >/dev/null 2>&1; then
        GENERATED_TOKEN="$(openssl rand -hex 32)"
        sed -i "s|^TRADING_AGENT_TOKEN=.*|TRADING_AGENT_TOKEN=${GENERATED_TOKEN}|" "$ENV_TRADING"
        unset GENERATED_TOKEN
        echo "  Generated a random TRADING_AGENT_TOKEN (value not printed)."
    else
        echo "  WARNING: openssl not found — set TRADING_AGENT_TOKEN by hand before starting."
    fi
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

echo "=== [1b/5] Enabling the credential pre-commit hook ==="
# Blocks a credential at commit time rather than catching it in CI after it is
# already pushed to a public repo. Cheap, idempotent, and the one guard that
# acts before the mistake leaves the machine.
git -C "$REPO_DIR" config core.hooksPath .githooks 2>/dev/null \
    && echo "core.hooksPath -> .githooks" \
    || echo "  (not a git checkout; skipping hook wiring)"

echo "=== [2b/5] Refusing to deploy a placeholder ==="
# Every variable below is a credential or an identity: it is wrong for it to
# still equal the value committed in the .example file. Everything NOT listed
# (hosts, ports, region, VESPER_TRADING=0, notional caps) is ordinary config
# that is *supposed* to match the example, so comparing whole files would cry
# wolf. Values are compared but never printed — only the offending NAME is,
# preserving this script's "never echoes credential values" property.
MUST_DIFFER_FROM_EXAMPLE=(
    "TRADING_AGENT_TOKEN"
    "WEBULL_APP_KEY"
    "WEBULL_APP_SECRET"
    "TD_API_KEY"
    "TDPRO_API_KEY"
    "SEC_USER_AGENT"
    "TELEGRAM_BOT_TOKEN"
    "TELEGRAM_AUTHORIZED_USER_IDS"
    "DISCORD_BOT_TOKEN"
)

placeholder_value_of() {   # $1=var  $2=file  -> prints value or nothing
    grep -E "^$1=" "$2" 2>/dev/null | head -n1 | cut -d= -f2- || true
}

STALE_NAMES=()
for pair in "$ENV_TRADING:$REPO_DIR/.env.trading-agent.example" \
            "$ENV_VESPER:$REPO_DIR/.env.vesper.example"; do
    live_file="${pair%%:*}"
    example_file="${pair##*:}"
    [ -f "$live_file" ] && [ -f "$example_file" ] || continue
    for var in "${MUST_DIFFER_FROM_EXAMPLE[@]}"; do
        live_value="$(placeholder_value_of "$var" "$live_file")"
        example_value="$(placeholder_value_of "$var" "$example_file")"
        if [ -n "$live_value" ] && [ "$live_value" = "$example_value" ]; then
            STALE_NAMES+=("$(basename "$live_file"):$var")
        fi
    done
done

if [ ${#STALE_NAMES[@]} -gt 0 ]; then
    echo "ERROR: these still hold the value committed in the .example file:"
    printf '  - %s\n' "${STALE_NAMES[@]}"
    echo ""
    echo "A placeholder must never become a live credential. Set each to a real"
    echo "value (a secret: openssl rand -hex 32) and re-run. Nothing has been"
    echo "installed or started."
    exit 1
fi
echo "No placeholder values found in either env contract."

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
