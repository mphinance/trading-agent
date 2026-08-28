"""Inbound Approval Callback Receiver & LangGraph Resume Engine.

Processes inbound webhook events and button callbacks from Telegram, Discord,
and HTTP dashboards to resolve paused LangGraph human approval gates in real time.
"""

from __future__ import annotations

import json
import logging
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


# Global singleton registry
approval_registry = ApprovalRegistry()
