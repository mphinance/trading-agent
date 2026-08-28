"""Central Approval Channel Manager (Multiplexing Telegram, Discord, and Webhooks)."""

from __future__ import annotations

import logging
from typing import List, Optional, Dict, Any

from vesper.bot.base import ApprovalChannel, ProposalCard
from vesper.bot.telegram_adapter import TelegramAdapter
from vesper.bot.discord_adapter import DiscordAdapter
from vesper.bot.webhook_adapter import WebhookAdapter
from vesper.state import OrderProposal, ExecutionResult

logger = logging.getLogger(__name__)


class ChannelManager:
    """Manages active notification and approval channels."""

    def __init__(self, channels: Optional[List[ApprovalChannel]] = None):
        if channels is not None:
            self.channels = channels
        else:
            # Auto-discover configured adapters
            self.channels = [
                TelegramAdapter(),
                DiscordAdapter(),
                WebhookAdapter(),
            ]

    @property
    def active_channels(self) -> List[ApprovalChannel]:
        """Returns only channels that have valid credentials set."""
        return [c for c in self.channels if c.configured]

    async def broadcast_proposal(self, prop: OrderProposal, thesis: str = "") -> Dict[str, Optional[str]]:
        """Broadcast an interactive proposal card across all configured channels."""
        card = ProposalCard.from_proposal(prop, thesis)
        results = {}
        for chan in self.active_channels:
            try:
                card_id = await chan.send_proposal_card(card)
                results[chan.channel_name] = card_id
            except Exception as e:
                logger.error(f"Failed to send proposal on {chan.channel_name}: {e}")
                results[chan.channel_name] = None
        return results

    async def broadcast_execution(self, result: ExecutionResult) -> Dict[str, bool]:
        """Broadcast execution confirmation across all active channels."""
        results = {}
        for chan in self.active_channels:
            try:
                ok = await chan.send_execution_result(result)
                results[chan.channel_name] = ok
            except Exception as e:
                logger.error(f"Failed to send execution result on {chan.channel_name}: {e}")
                results[chan.channel_name] = False
        return results

    async def broadcast_alert(self, title: str, message: str, level: str = "INFO") -> Dict[str, bool]:
        """Broadcast a general alert across all active channels."""
        results = {}
        for chan in self.active_channels:
            try:
                ok = await chan.send_alert(title, message, level)
                results[chan.channel_name] = ok
            except Exception as e:
                logger.error(f"Failed to broadcast alert on {chan.channel_name}: {e}")
                results[chan.channel_name] = False
        return results


# Global singleton manager
channel_manager = ChannelManager()
