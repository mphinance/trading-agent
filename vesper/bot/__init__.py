"""Vesper Channel-Agnostic Bot & Approval Engine."""

from vesper.bot.base import ApprovalChannel, ProposalCard
from vesper.bot.telegram_adapter import TelegramAdapter
from vesper.bot.discord_adapter import DiscordAdapter
from vesper.bot.webhook_adapter import WebhookAdapter
from vesper.bot.manager import ChannelManager, channel_manager

__all__ = [
    "ApprovalChannel",
    "ProposalCard",
    "TelegramAdapter",
    "DiscordAdapter",
    "WebhookAdapter",
    "ChannelManager",
    "channel_manager",
]
