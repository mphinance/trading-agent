"""Inbound Approval Callback Receiver & LangGraph Resume Engine.

Processes inbound webhook events and button callbacks from Telegram, Discord,
and HTTP dashboards to resolve paused LangGraph human approval gates in real time.

The `ApprovalRegistry` class itself (persistence, CRUD, and payload parsing
via `handle_callback_payload`) moved to `core/approval_registry.py` in M0-04
-- it has no dependency on aiohttp or on this package's Telegram/Discord
adapter classes, and callers that only need to read pending approvals (e.g.
trading_mcp/vesper_tools.py's read-only MCP tools) should not have to import
`vesper.bot` -- which eagerly constructs TelegramAdapter/DiscordAdapter/
WebhookAdapter/ChannelManager via `vesper/bot/__init__.py` -- just to get at
it. What's left here is genuinely webhook-specific: the HMAC/Ed25519/bearer
guards that authenticate an inbound HTTP request, and the aiohttp
Application + routes that receive Telegram/Discord/REST callbacks and hand
the parsed payload to the shared `approval_registry` singleton.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Any, Optional

from core.approval_registry import ApprovalRegistry, approval_registry

logger = logging.getLogger(__name__)


# ── Cryptographic & Secret Authentication Guards ──────────────────────────────
#
# All three guards below fail CLOSED when unconfigured, not open. An earlier
# version of this file returned True ("authorized") whenever the relevant
# secret env var was unset — meaning a deploy that forgot to set
# TELEGRAM_WEBHOOK_SECRET/DISCORD_PUBLIC_KEY/VESPER_WEBHOOK_SECRET would
# silently accept unauthenticated approve/reject/halt commands from anyone
# who could reach the port. This repo's own deploy/install.sh refuses to run
# without a Tailscale IP rather than falling back to something less safe —
# these guards follow the same rule: no configured secret means the route is
# unusable, not open.

def verify_telegram_webhook_secret(secret_token_header: Optional[str]) -> bool:
    """Verify Telegram X-Telegram-Bot-Api-Secret-Token against environment configuration."""
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not expected:
        logger.warning("TELEGRAM_WEBHOOK_SECRET not configured — rejecting all Telegram webhook calls.")
        return False
    return bool(secret_token_header) and hmac.compare_digest(secret_token_header.strip(), expected)


def verify_discord_signature(signature: Optional[str], timestamp: Optional[str], body: bytes) -> bool:
    """Verify Discord Ed25519 request signature using cryptographic public key."""
    pub_key_hex = os.getenv("DISCORD_PUBLIC_KEY", "").strip()
    if not pub_key_hex:
        logger.warning("DISCORD_PUBLIC_KEY not configured — rejecting all Discord webhook calls.")
        return False
    if not signature or not timestamp:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_key_hex))
        message = timestamp.encode("utf-8") + body
        sig_bytes = bytes.fromhex(signature)
        pub_key.verify(sig_bytes, message)
        return True
    except Exception as e:
        logger.warning(f"Discord Ed25519 signature verification failed: {e}")
        return False


def verify_bearer_token(auth_header: Optional[str]) -> bool:
    """Verify REST API Bearer token against VESPER_WEBHOOK_SECRET or VESPER_API_TOKEN."""
    expected = os.getenv("VESPER_WEBHOOK_SECRET", "") or os.getenv("VESPER_API_TOKEN", "")
    expected = expected.strip()
    if not expected:
        logger.warning("VESPER_WEBHOOK_SECRET/VESPER_API_TOKEN not configured — rejecting all REST approval calls.")
        return False
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:].strip()
    return hmac.compare_digest(token, expected)


# ── Webhook HTTP Server Factory (aiohttp) ────────────────────────────────────

def create_inbound_app() -> Any:
    """Create configured aiohttp Application for inbound webhook routes."""
    try:
        from aiohttp import web
    except ImportError:
        logger.warning("aiohttp not installed, inbound webhook server unavailable.")
        return None

    app = web.Application()

    async def handle_telegram(request: web.Request) -> web.Response:
        sec = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not verify_telegram_webhook_secret(sec):
            return web.json_response({"error": "Unauthorized secret token"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        res = await approval_registry.handle_callback_payload(payload)
        return web.json_response(res)

    async def handle_discord(request: web.Request) -> web.Response:
        sig = request.headers.get("X-Signature-Ed25519")
        ts = request.headers.get("X-Signature-Timestamp")
        raw_body = await request.read()
        if not verify_discord_signature(sig, ts, raw_body):
            return web.json_response({"error": "Invalid signature"}, status=401)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        # Discord PING ACK handshake (type 1)
        if payload.get("type") == 1:
            return web.json_response({"type": 1})

        res = await approval_registry.handle_callback_payload(payload)
        return web.json_response({"type": 4, "data": {"content": f"Decision processed: {res.get('status')}"}})

    async def handle_rest_approval(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization")
        if not verify_bearer_token(auth):
            return web.json_response({"error": "Unauthorized bearer token"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        res = await approval_registry.handle_callback_payload(payload)
        return web.json_response(res)

    async def handle_health(request: web.Request) -> web.Response:
        # Deliberately unauthenticated (infra liveness probes don't send auth
        # headers) and deliberately minimal — no pending-proposal details here.
        # A ticker/side/quantity/price list is exactly what an unauthenticated
        # GET should never hand out; see handle_approvals for the guarded view.
        from core.halt import is_halted
        halted, _details = is_halted()
        return web.json_response({"status": "ok", "is_halted": halted})

    async def handle_approvals(request: web.Request) -> web.Response:
        # Pending proposals name real trade intentions (ticker/side/quantity/
        # price) — same auth as the REST approval route, not the open liveness
        # probe above.
        auth = request.headers.get("Authorization")
        if not verify_bearer_token(auth):
            return web.json_response({"error": "Unauthorized bearer token"}, status=401)
        return web.json_response({
            "pending_approvals_count": len(approval_registry.list_pending()),
            "pending_approvals": approval_registry.list_pending(),
        })

    app.router.add_post("/webhook/telegram", handle_telegram)
    app.router.add_post("/webhook/discord", handle_discord)
    app.router.add_post("/webhook/approval", handle_rest_approval)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/approvals", handle_approvals)

    return app


__all__ = [
    "ApprovalRegistry",
    "approval_registry",
    "verify_telegram_webhook_secret",
    "verify_discord_signature",
    "verify_bearer_token",
    "create_inbound_app",
]
