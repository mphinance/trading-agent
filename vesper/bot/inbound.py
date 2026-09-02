"""Inbound Approval Callback Receiver & LangGraph Resume Engine.

Processes inbound webhook events and button callbacks from Telegram, Discord,
and HTTP dashboards to resolve paused LangGraph human approval gates in real time.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_APPROVAL_STATE_PATH = _DATA_DIR / "approval_registry_state.json"


def _load_approval_state() -> Dict[str, Any]:
    if not _APPROVAL_STATE_PATH.exists():
        return {"pending": {}, "decisions": {}}
    try:
        with open(_APPROVAL_STATE_PATH) as f:
            state = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read approval registry state file: {e}")
        return {"pending": {}, "decisions": {}}
    state.setdefault("pending", {})
    state.setdefault("decisions", {})
    return state


def _save_approval_state(state: Dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _APPROVAL_STATE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, _APPROVAL_STATE_PATH)

# Per-user authorization for Telegram callbacks -- separate from (and in
# addition to) the aiohttp webhook route's HMAC/secret-token checks below.
# Those prove a request really came from Telegram's servers; they say
# nothing about WHICH Telegram user tapped the button or typed /halt. Same
# gap, same fix shape as the one found in vesper/bot/discord_gateway.py's
# ApprovalButton/on_message (any user who could see the channel could
# approve/reject any proposal or halt/resume trading) -- unset here defaults
# to allow-with-a-warning rather than fail-closed, matching that module's
# design choice for a single-operator deployment where requiring
# configuration up front would just be friction.
_AUTHORIZED_TELEGRAM_USER_IDS = {
    s.strip() for s in os.getenv("TELEGRAM_AUTHORIZED_USER_IDS", "").split(",") if s.strip()
}
_warned_telegram_unrestricted = False


def _is_telegram_user_authorized(user_id: Any) -> bool:
    global _warned_telegram_unrestricted
    if not _AUTHORIZED_TELEGRAM_USER_IDS:
        if not _warned_telegram_unrestricted:
            logger.warning(
                "TELEGRAM_AUTHORIZED_USER_IDS is not set — any Telegram user who can "
                "message this bot can approve/reject proposals and halt/resume trading. "
                "Set it to a comma-separated list of Telegram numeric user IDs to restrict this."
            )
            _warned_telegram_unrestricted = True
        return True
    return str(user_id) in _AUTHORIZED_TELEGRAM_USER_IDS


# Per-user authorization for Discord webhook interactions -- reuses DISCORD_AUTHORIZED_USER_IDS
# defined in vesper/bot/discord_gateway.py.
_AUTHORIZED_DISCORD_USER_IDS = {
    s.strip() for s in os.getenv("DISCORD_AUTHORIZED_USER_IDS", "").split(",") if s.strip()
}
_warned_discord_unrestricted = False


def _is_discord_user_authorized(user_id: Any) -> bool:
    global _warned_discord_unrestricted
    if not _AUTHORIZED_DISCORD_USER_IDS:
        if not _warned_discord_unrestricted:
            logger.warning(
                "DISCORD_AUTHORIZED_USER_IDS is not set — any Discord user who can "
                "trigger interactions can approve/reject proposals. "
                "Set it to a comma-separated list of Discord user IDs to restrict this."
            )
            _warned_discord_unrestricted = True
        return True
    return str(user_id) in _AUTHORIZED_DISCORD_USER_IDS


class ApprovalRegistry:
    """Registry for pending order proposals awaiting human resolution.

    Disk-backed (JSON, atomic write -- same pattern as core/halt.py),
    not an in-memory dict. A pending proposal and the decision that resolves
    it both need to survive a process restart: `vesper loop` is a long-lived
    daemon now, and a proposal drafted from a scheduled scan can genuinely
    be sitting in a Telegram/Discord chat waiting for a tap when the process
    crashes or gets redeployed. An in-memory-only registry would silently
    strand that proposal -- the tap would arrive and find nothing to
    resolve. See vesper/graph.py's persistent checkpointer for the other
    half of this: the pending record alone isn't enough without the
    LangGraph thread it points at also surviving a restart.
    """

    def __init__(self) -> None:
        self._graph_app: Optional[Any] = None

    def set_graph_app(self, app: Any) -> None:
        """Register compiled LangGraph StateGraph instance for resume invocations.

        Not persisted (and can't be -- it's a live compiled graph object,
        rebuilt fresh by whatever process starts next). As long as the fresh
        graph is built against the same persistent checkpointer file
        (vesper/graph.py's default), resuming a thread_id from a PRIOR
        process's paused run still works: the checkpointer, not this
        Python object, is what actually remembers the paused state.
        """
        self._graph_app = app

    def register_pending(
        self,
        proposal_id: str,
        session_id: str,
        thread_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register an order proposal awaiting human approval."""
        state = _load_approval_state()
        state["pending"][proposal_id] = {
            "proposal_id": proposal_id,
            "session_id": session_id,
            "thread_id": thread_id or session_id,
            "details": details or {},
            "status": "PENDING",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_approval_state(state)
        logger.info(f"📋 Registered pending approval for proposal {proposal_id} (Session: {session_id})")

    def get_pending(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        return _load_approval_state()["pending"].get(proposal_id)

    def get_decision(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        return _load_approval_state()["decisions"].get(proposal_id)

    def list_pending(self) -> list[Dict[str, Any]]:
        return [v for v in _load_approval_state()["pending"].values() if v.get("status") == "PENDING"]

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
            from core.halt import halt
            halt_res = halt(reason=f"Emergency freeze from {source} by {user_id}", source=source)
            return {
                "status": "HALTED",
                "proposal_id": proposal_id,
                "decision": "HALT",
                "message": halt_res["message"],
            }

        state = _load_approval_state()

        # First decision wins, permanently. A proposal can be resolved twice in
        # ordinary use -- Telegram/Discord redeliver callbacks, a tap can be
        # double-registered, and more than one authorised user can be looking at
        # the same card. LangGraph itself is safe here (verified: a duplicate
        # Command(resume=...) on a completed thread does NOT re-execute, so
        # there is no double-order risk), but the RECORD was not: this used to
        # overwrite state["decisions"][proposal_id] unconditionally, so a REJECT
        # landing after an APPROVE had already executed would rewrite the audit
        # trail to say the order was rejected. The trail would then contradict
        # what the broker actually did, which is the one thing it exists to
        # prevent. Later decisions are logged and refused, never applied.
        prior = state["decisions"].get(proposal_id)
        if prior:
            logger.warning(
                f"Ignoring duplicate decision {decision_clean} for {proposal_id} from "
                f"{source}/{user_id} — already resolved as {prior.get('decision')} by "
                f"{prior.get('source')}/{prior.get('user_id')} at {prior.get('resolved_at')}"
            )
            return {
                "status": "ALREADY_RESOLVED",
                "proposal_id": proposal_id,
                "decision": prior.get("decision"),
                "resolved_at": prior.get("resolved_at"),
                "resolved_by": f"{prior.get('source')}/{prior.get('user_id')}",
                "ignored": decision_clean,
            }

        item = state["pending"].get(proposal_id)
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

        state["decisions"][proposal_id] = record
        _save_approval_state(state)

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
            from_user = cb.get("from", {})
            user = from_user.get("username", "telegram_user")
            user_id = from_user.get("id")
            if not _is_telegram_user_authorized(user_id):
                logger.warning(f"Rejected Telegram callback from unauthorized user {user} (id={user_id})")
                return {
                    "status": "UNAUTHORIZED",
                    "message": f"User {user} is not authorized to resolve proposals.",
                }
            if ":" in data_str:
                action, prop_id = data_str.split(":", 1)
                decision = "APPROVE" if action.lower() == "approve" else "REJECT"
                return await self.submit_decision(prop_id, decision, source="telegram", user_id=user)

        # 2. Telegram Text Message (e.g. /halt or /resume)
        if "message" in payload and "text" in payload["message"]:
            text = str(payload["message"]["text"]).strip()
            from_user = payload["message"].get("from", {})
            user = from_user.get("username", "telegram_user")
            user_id = from_user.get("id")
            if text.startswith("/halt") or text.startswith("/resume"):
                if not _is_telegram_user_authorized(user_id):
                    logger.warning(f"Rejected Telegram {text.split()[0]} from unauthorized user {user} (id={user_id})")
                    return {
                        "status": "UNAUTHORIZED",
                        "message": f"User {user} is not authorized to halt/resume trading.",
                    }
            if text.startswith("/halt"):
                from core.halt import halt
                return halt(reason=f"Telegram /halt from {user}", source="telegram")
            if text.startswith("/resume"):
                from core.halt import resume
                return resume(source="telegram")

        # 3. Discord Interaction
        if payload.get("type") == 3 and "data" in payload:  # Message Component Interaction
            custom_id = str(payload["data"].get("custom_id", ""))
            user = payload.get("member", {}).get("user", {}).get("username", "discord_user")
            user_id = payload.get("member", {}).get("user", {}).get("id")
            if not _is_discord_user_authorized(user_id):
                logger.warning(f"Rejected Discord interaction from unauthorized user {user} (id={user_id})")
                return {
                    "status": "UNAUTHORIZED",
                    "message": f"User {user} is not authorized to resolve proposals.",
                }
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
            from core.halt import halt
            reason = payload.get("reason", "API halt trigger")
            return halt(reason=reason, source=source)

        if payload.get("command") == "resume" or payload.get("action") == "resume":
            from core.halt import resume
            return resume(source=source)

        return {"status": "ERROR", "message": "Unrecognized callback payload format."}


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


# Global singleton registry
approval_registry = ApprovalRegistry()

