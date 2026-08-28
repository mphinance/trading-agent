"""Generic JSON Webhook Adapter for Custom Dashboards / Automations."""

from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any
import httpx
from dataclasses import asdict

from vesper.bot.base import ApprovalChannel, ProposalCard
from vesper.state import ExecutionResult

logger = logging.getLogger(__name__)


class WebhookAdapter(ApprovalChannel):
    """Generic JSON HTTP Webhook channel adapter."""

    def __init__(self, endpoint_url: Optional[str] = None):
        self.endpoint_url = endpoint_url or os.getenv("VESPER_WEBHOOK_URL", "")
        self._client = httpx.AsyncClient(timeout=10.0)

    @property
    def channel_name(self) -> str:
        return "webhook"

    @property
    def configured(self) -> bool:
        return bool(self.endpoint_url and not self.endpoint_url.startswith("your_"))

    async def send_proposal_card(self, card: ProposalCard) -> Optional[str]:
        if not self.configured:
            return None
        payload = {
            "event": "PROPOSAL_CREATED",
            "card": asdict(card),
        }
        try:
            resp = await self._client.post(self.endpoint_url, json=payload)
            if resp.status_code in [200, 201, 202, 204]:
                return card.proposal_id
            return None
        except Exception as e:
            logger.error(f"Webhook send_proposal_card error: {e}")
            return None

    async def send_execution_result(self, result: ExecutionResult) -> bool:
        if not self.configured:
            return False
        payload = {
            "event": "ORDER_EXECUTED",
            "result": result.model_dump(),
        }
        try:
            resp = await self._client.post(self.endpoint_url, json=payload)
            return resp.status_code in [200, 201, 202, 204]
        except Exception as e:
            logger.error(f"Webhook send_execution_result error: {e}")
            return False

    async def send_alert(self, title: str, message: str, level: str = "INFO") -> bool:
        if not self.configured:
            return False
        payload = {
            "event": "MARKET_ALERT",
            "level": level,
            "title": title,
            "message": message,
        }
        try:
            resp = await self._client.post(self.endpoint_url, json=payload)
            return resp.status_code in [200, 201, 202, 204]
        except Exception as e:
            logger.error(f"Webhook send_alert error: {e}")
            return False
