"""Inbound Approval Callback Receiver & LangGraph Resume Engine.

Processes inbound webhook events and button callbacks from Telegram, Discord,
and HTTP dashboards to resolve paused LangGraph human approval gates in real time.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ApprovalRegistry:
    """Thread-safe registry for pending order proposals awaiting human resolution."""

    def __init__(self) -> None:
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._decisions: Dict[str, Dict[str, Any]] = {}
        self._graph_app: Optional[Any] = None

    def set_graph_app(self, app: Any) -> None:
        """Register compiled LangGraph StateGraph instance for resume invocations."""
        self._graph_app = app

    def register_pending(
        self,
        proposal_id: str,
        session_id: str,
        thread_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register an order proposal awaiting human approval."""
        self._pending[proposal_id] = {
            "proposal_id": proposal_id,
            "session_id": session_id,
            "thread_id": thread_id or session_id,
            "details": details or {},
            "status": "PENDING",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"📋 Registered pending approval for proposal {proposal_id} (Session: {session_id})")

    def get_pending(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        return self._pending.get(proposal_id)

    def get_decision(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        return self._decisions.get(proposal_id)

    def list_pending(self) -> list[Dict[str, Any]]:
        return [v for v in self._pending.values() if v.get("status") == "PENDING"]

    async def submit_decision(
        self,
        proposal_id: str,
        decision: str,
        source: str = "webhook",
        user_id: str = "human",
    ) -> Dict[str, Any]:
        """Submit a decision ('APPROVE', 'REJECT', 'HALT') for a pending proposal."""
        decision_clean = decision.strip().upper()
        now = datetime.now(timezone.utc).isoformat()

        # Handle emergency halt trigger
        if decision_clean in ("HALT", "FREEZE", "KILL"):
            from vesper.halt import halt
            halt_res = halt(reason=f"Emergency freeze from {source} by {user_id}", source=source)
            return {
                "status": "HALTED",
                "proposal_id": proposal_id,
                "decision": "HALT",
                "message": halt_res["message"],
            }

        item = self._pending.get(proposal_id)
        record = {
            "proposal_id": proposal_id,
            "decision": decision_clean,
            "source": source,
            "user_id": user_id,
            "resolved_at": now,
            "session_id": item.get("session_id") if item else None,
            "thread_id": item.get("thread_id") if item else None,
        }

        if item:
            item["status"] = decision_clean
            item["resolved_at"] = now
            item["decision"] = decision_clean

        self._decisions[proposal_id] = record

        # Resume LangGraph thread if active graph and thread_id exist
        resumed = False
        if self._graph_app and item and item.get("thread_id"):
            try:
                from langgraph.types import Command
                thread_id = item["thread_id"]
                logger.info(f"🔄 Resuming LangGraph thread {thread_id} with decision {decision_clean}")
                await self._graph_app.ainvoke(
                    Command(resume=decision_clean),
                    config={"configurable": {"thread_id": thread_id}},
                )
                resumed = True
            except Exception as e:
                logger.warning(f"Could not auto-resume LangGraph thread: {e}")

        logger.info(
            f"✅ Proposal {proposal_id} resolved: {decision_clean} via {source} by {user_id} (Resumed Graph: {resumed})"
        )
        return {
            "status": "RESOLVED",
            "proposal_id": proposal_id,
            "decision": decision_clean,
            "resumed_graph": resumed,
            "timestamp": now,
        }

    async def handle_callback_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Parse raw incoming payloads from Telegram, Discord, or generic Webhooks."""
        # 1. Telegram Callback Query
        if "callback_query" in payload:
            cb = payload["callback_query"]
            data_str = str(cb.get("data", ""))
            user = cb.get("from", {}).get("username", "telegram_user")
            if ":" in data_str:
                action, prop_id = data_str.split(":", 1)
                decision = "APPROVE" if action.lower() == "approve" else "REJECT"
                return await self.submit_decision(prop_id, decision, source="telegram", user_id=user)

        # 2. Telegram Text Message (e.g. /halt or /resume)
        if "message" in payload and "text" in payload["message"]:
            text = str(payload["message"]["text"]).strip()
            user = payload["message"].get("from", {}).get("username", "telegram_user")
            if text.startswith("/halt"):
                from vesper.halt import halt
                return halt(reason=f"Telegram /halt from {user}", source="telegram")
            if text.startswith("/resume"):
                from vesper.halt import resume
                return resume(source="telegram")

        # 3. Discord Interaction
        if payload.get("type") == 3 and "data" in payload:  # Message Component Interaction
            custom_id = str(payload["data"].get("custom_id", ""))
            user = payload.get("member", {}).get("user", {}).get("username", "discord_user")
            if ":" in custom_id:
                action, prop_id = custom_id.split(":", 1)
                decision = "APPROVE" if action.lower() == "approve" else "REJECT"
                return await self.submit_decision(prop_id, decision, source="discord", user_id=user)

        # 4. Generic REST Webhook
        prop_id = payload.get("proposal_id") or payload.get("id")
        decision = payload.get("decision") or payload.get("action")
        user = payload.get("user") or payload.get("user_id") or "api_client"
        source = payload.get("source") or "webhook"

        if prop_id and decision:
            return await self.submit_decision(str(prop_id), str(decision), source=source, user_id=user)

        # 5. Direct /halt or /resume POST payload
        if payload.get("command") == "halt" or payload.get("action") == "halt":
            from vesper.halt import halt
            reason = payload.get("reason", "API halt trigger")
            return halt(reason=reason, source=source)

        if payload.get("command") == "resume" or payload.get("action") == "resume":
            from vesper.halt import resume
            return resume(source=source)

        return {"status": "ERROR", "message": "Unrecognized callback payload format."}


# ── Cryptographic & Secret Authentication Guards ──────────────────────────────

def verify_telegram_webhook_secret(secret_token_header: Optional[str]) -> bool:
    """Verify Telegram X-Telegram-Bot-Api-Secret-Token against environment configuration."""
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not expected:
        return True  # Auth not required if secret is unconfigured
    return bool(secret_token_header and secret_token_header.strip() == expected)


def verify_discord_signature(signature: Optional[str], timestamp: Optional[str], body: bytes) -> bool:
    """Verify Discord Ed25519 request signature using cryptographic public key."""
    pub_key_hex = os.getenv("DISCORD_PUBLIC_KEY", "").strip()
    if not pub_key_hex:
        return True  # Auth not required if public key is unconfigured
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
        return True  # Auth not required if token is unconfigured
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:].strip()
    return token == expected


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
        from vesper.halt import is_halted
        halted, details = is_halted()
        return web.json_response({
            "status": "ok",
            "is_halted": halted,
            "pending_approvals_count": len(approval_registry.list_pending()),
            "pending_approvals": approval_registry.list_pending(),
        })

    app.router.add_post("/webhook/telegram", handle_telegram)
    app.router.add_post("/webhook/discord", handle_discord)
    app.router.add_post("/webhook/approval", handle_rest_approval)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/approvals", handle_health)

    return app


# Global singleton registry
approval_registry = ApprovalRegistry()

