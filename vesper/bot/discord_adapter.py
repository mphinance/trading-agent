"""Discord Webhook & Bot Adapter for Interactive Trade Approvals."""

from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any
import httpx

from vesper.bot.base import ApprovalChannel, ProposalCard
from vesper.state import ExecutionResult

logger = logging.getLogger(__name__)


class DiscordAdapter(ApprovalChannel):
    """Discord Webhook / Bot channel adapter."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        bot_token: Optional[str] = None,
    ):
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")
        self.bot_token = bot_token or os.getenv("DISCORD_BOT_TOKEN", "")
        self._client = httpx.AsyncClient(timeout=10.0)

    @property
    def channel_name(self) -> str:
        return "discord"

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url and not self.webhook_url.startswith("your_"))

    async def send_proposal_card(self, card: ProposalCard) -> Optional[str]:
        """Send proposal embed to Discord channel."""
        if not self.configured:
            logger.debug("Discord not configured. Skipping proposal card.")
            return None

        # Rich Discord Embed (Neon Cyan / Gold aesthetic)
        embed = {
            "title": f"⚡ VESPER TRADE PROPOSAL: {card.side.upper()} {card.ticker}",
            "description": card.thesis or "Quantitative setup criteria met.",
            "color": 0x00F0FF if card.side.upper() == "BUY" else 0xFF0055,
            "fields": [
                {"name": "Action", "value": f"**{card.side.upper()} {card.quantity}x** @ ${card.limit_price:.2f}", "inline": True},
                {"name": "Asset Type", "value": card.asset_type, "inline": True},
                {"name": "Est. Cost", "value": f"${card.est_cost:,.2f}", "inline": True},
                {"name": "Max Risk", "value": f"${card.max_risk_usd:,.2f}", "inline": True},
                {"name": "Stop Loss", "value": f"${card.stop_loss:.2f}" if card.stop_loss else "N/A", "inline": True},
                {"name": "Target", "value": f"${card.target_price:.2f}" if card.target_price else "N/A", "inline": True},
                {"name": "Proposal ID", "value": f"`{card.proposal_id}`", "inline": False},
            ],
            "footer": {"text": "Vesper Quant Engine • Reply with approve / reject"},
        }

        try:
            resp = await self._client.post(
                self.webhook_url,
                json={"username": "Vesper Quant Bot", "embeds": [embed]},
            )
            if resp.status_code in [200, 204]:
                logger.info(f"Sent Discord proposal card for {card.proposal_id}")
                return card.proposal_id
            logger.error(f"Discord webhook failed with code {resp.status_code}: {resp.text}")
            return None
        except Exception as e:
            logger.error(f"Discord send_proposal_card error: {e}")
            return None

    async def send_execution_result(self, result: ExecutionResult) -> bool:
        if not self.configured:
            return False

        color = 0x00FF88 if result.status in ["SUBMITTED", "DRY_RUN_SIMULATED"] else 0xFF0055
        embed = {
            "title": f"⚡ EXECUTION REPORT: {result.ticker} ({result.status})",
            "description": result.message,
            "color": color,
            "fields": [
                {"name": "Proposal ID", "value": f"`{result.order_proposal_id}`", "inline": True},
                {"name": "Status", "value": result.status, "inline": True},
                {"name": "Order ID", "value": f"`{result.client_order_id}`", "inline": True},
                {"name": "Filled Qty", "value": str(result.filled_quantity), "inline": True},
                {"name": "Filled Price", "value": f"${result.filled_price:.2f}" if result.filled_price else "N/A", "inline": True},
                {"name": "Timestamp", "value": result.timestamp, "inline": False},
            ],
            "footer": {"text": "Vesper Quant Execution Engine"},
        }

        try:
            resp = await self._client.post(
                self.webhook_url,
                json={"username": "Vesper Quant Bot", "embeds": [embed]},
            )
            return resp.status_code in [200, 204]
        except Exception as e:
            logger.error(f"Discord send_execution_result error: {e}")
            return False

    async def send_alert(self, title: str, message: str, level: str = "INFO") -> bool:
        if not self.configured:
            return False
        color = 0xFFAA00 if level == "WARNING" else (0xFF0055 if level == "ERROR" else 0x00F0FF)
        embed = {
            "title": title,
            "description": message,
            "color": color,
            "footer": {"text": "Vesper Market Alert"},
        }
        try:
            resp = await self._client.post(
                self.webhook_url,
                json={"username": "Vesper Quant Bot", "embeds": [embed]},
            )
            return resp.status_code in [200, 204]
        except Exception as e:
            logger.error(f"Discord send_alert error: {e}")
            return False
