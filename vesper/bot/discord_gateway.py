"""Persistent Discord Gateway Client & Dynamic Approval Button Router (Module 2).

Uses discord.py Client connected over outbound WebSocket gateway with
discord.ui.DynamicItem for stateless, timeout-free Approve/Reject buttons.
Matches the outbound-only, no-inbound-port security model of Telegram long-polling.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from typing import Any, Dict, Optional

import discord

from vesper.bot.base import ProposalCard

logger = logging.getLogger(__name__)

# Pattern: vesper|approve|<proposal_id> or vesper|reject|<proposal_id>
APPROVAL_REGEX = r"vesper\|(?P<action>approve|reject)\|(?P<proposal_id>.+)"

# Optional allowlist of Discord user (snowflake) IDs permitted to approve/
# reject a proposal or trigger /halt /resume. Discord bots are commonly
# added to multi-member servers -- without this, *anyone* who can see the
# channel can approve a trade proposal (still bounded by execution_guard's
# caps, but it's real money) or freeze/unfreeze trading. Empty means
# unrestricted -- matches this repo's other opt-in guard patterns (e.g.
# VESPER_SYMBOL_ALLOWLIST) -- but warns loudly rather than silently.
_AUTHORIZED_USER_IDS = {
    s.strip() for s in os.getenv("DISCORD_AUTHORIZED_USER_IDS", "").split(",") if s.strip()
}
_warned_unrestricted = False


def _is_authorized(user_id: str) -> bool:
    global _warned_unrestricted
    if not _AUTHORIZED_USER_IDS:
        if not _warned_unrestricted:
            logger.warning(
                "DISCORD_AUTHORIZED_USER_IDS is not set -- any user who can see this "
                "channel/server can approve/reject proposals and trigger /halt /resume. "
                "Set it to a comma-separated list of Discord user IDs to restrict this."
            )
            _warned_unrestricted = True
        return True
    return user_id in _AUTHORIZED_USER_IDS


class ApprovalButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=APPROVAL_REGEX,
):
    """Stateless dynamic item: custom_id encodes action and proposal_id,
    allowing button interactions to survive bot restarts and view timeouts."""

    def __init__(
        self,
        action: str,
        proposal_id: str,
        *,
        label: Optional[str] = None,
        style: Optional[discord.ButtonStyle] = None,
    ) -> None:
        self.action = action.lower()
        self.proposal_id = proposal_id
        if style is None:
            style = discord.ButtonStyle.green if self.action == "approve" else discord.ButtonStyle.red
        if label is None:
            label = "✅ APPROVE & EXECUTE" if self.action == "approve" else "❌ REJECT"
        custom_id = f"vesper|{self.action}|{self.proposal_id}"[:100]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=custom_id))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> ApprovalButton:
        """Called by discord.py on every click, reconstructing button purely from regex match."""
        return cls(match["action"], match["proposal_id"], label="\u200b")

    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle button click and submit decision to Vesper's shared approval registry."""
        decision = "APPROVE" if self.action == "approve" else "REJECT"
        user_name = str(getattr(interaction.user, "name", interaction.user))
        user_id = str(getattr(interaction.user, "id", user_name))

        if not _is_authorized(user_id):
            logger.warning(f"Unauthorized Discord approval attempt by {user_name} ({user_id}) on {self.proposal_id}")
            try:
                await interaction.response.send_message(
                    "⛔ You're not authorized to approve/reject Vesper proposals.", ephemeral=True
                )
            except Exception:
                pass
            return

        # Submit decision into Vesper's shared approval registry
        from vesper.bot.inbound import approval_registry
        res = await approval_registry.submit_decision(
            proposal_id=self.proposal_id,
            decision=decision,
            source="discord",
            user_id=f"{user_name} ({user_id})",
        )

        icon = "✅" if decision == "APPROVE" else "❌"
        action_past = "APPROVED" if decision == "APPROVE" else "REJECTED"
        msg = f"{icon} Proposal `{self.proposal_id}` **{action_past}** by <@{user_id}>."
        if res.get("status") == "HALTED":
            msg = f"🚨 System HALTED by <@{user_id}>."

        # Acknowledge or respond to interaction
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            logger.warning(f"Failed to respond to Discord interaction: {e}")


def create_approval_view(proposal_id: str) -> discord.ui.View:
    """Create a persistent View containing stateless dynamic Approve/Reject buttons."""
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton("approve", proposal_id))
    view.add_item(ApprovalButton("reject", proposal_id))
    return view


def get_approval_components(proposal_id: str) -> list[dict[str, Any]]:
    """Return raw ActionRow component payload for Discord REST API."""
    return [
        {
            "type": 1,  # Action Row
            "components": [
                {
                    "type": 2,  # Button
                    "style": 3,  # Green / Success
                    "label": "✅ APPROVE & EXECUTE",
                    "custom_id": f"vesper|approve|{proposal_id}"[:100],
                },
                {
                    "type": 2,  # Button
                    "style": 4,  # Red / Danger
                    "label": "❌ REJECT",
                    "custom_id": f"vesper|reject|{proposal_id}"[:100],
                },
            ],
        }
    ]


class VesperDiscordBot(discord.Client):
    """Outbound Discord Gateway Client with DynamicItem approval routing."""

    def __init__(self, bot_token: Optional[str] = None) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.bot_token = bot_token if bot_token is not None else os.getenv("DISCORD_BOT_TOKEN", "")

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and not self.bot_token.startswith("your_"))

    async def setup_hook(self) -> None:
        """Register dynamic items prior to connecting."""
        self.add_dynamic_items(ApprovalButton)
        logger.info("Discord DynamicItem ApprovalButton registered.")

    async def on_ready(self) -> None:
        user_disc = getattr(self.user, "discriminator", "0")
        logger.info(
            f"Discord Gateway connected as {self.user.name}#{user_disc} (ID: {self.user.id})"
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.author == self.user:
            return

        text = message.content.strip()
        user_name = str(message.author.name)
        user_id = str(message.author.id)

        if not (text.startswith(("/halt", "!halt", "/resume", "!resume"))):
            return
        if not _is_authorized(user_id):
            logger.warning(f"Unauthorized Discord halt/resume attempt by {user_name} ({user_id})")
            await message.channel.send("⛔ You're not authorized to halt/resume Vesper trading.")
            return

        if text.startswith("/halt") or text.startswith("!halt"):
            from core.halt import halt
            reason = text.split(" ", 1)[1] if " " in text else "Emergency freeze via Discord"
            halt(reason=f"{reason} by {user_name} ({user_id})", source="discord")
            await message.channel.send(f"🚨 **EMERGENCY FREEZE ACTIVATED**: {reason}")
        elif text.startswith("/resume") or text.startswith("!resume"):
            from core.halt import resume
            resume()
            await message.channel.send("🟢 **SYSTEM RESUMED**: Trading operations unlocked.")

    async def send_proposal_card(
        self,
        channel_id: int | str,
        card: ProposalCard,
        chart_bytes: Optional[bytes] = None,
    ) -> Optional[str]:
        """Send proposal card with DynamicItem buttons directly over gateway."""
        try:
            channel = self.get_channel(int(channel_id))
            if not channel:
                channel = await self.fetch_channel(int(channel_id))
            if not channel:
                logger.error(f"Discord channel {channel_id} not found.")
                return None

            embed_color = 0x00F0FF if card.side.upper() == "BUY" else 0xFF0055
            embed = discord.Embed(
                title=f"⚡ VESPER TRADE PROPOSAL: {card.side.upper()} {card.ticker}",
                description=card.thesis or "Quantitative setup criteria met.",
                color=embed_color,
            )
            embed.add_field(name="Action", value=f"**{card.side.upper()} {card.quantity}x** @ ${card.limit_price:.2f}", inline=True)
            embed.add_field(name="Asset Type", value=card.asset_type, inline=True)
            embed.add_field(name="Est. Cost", value=f"${card.est_cost:,.2f}", inline=True)
            embed.add_field(name="Max Risk", value=f"${card.max_risk_usd:,.2f}", inline=True)
            embed.add_field(name="Stop Loss", value=f"${card.stop_loss:.2f}" if card.stop_loss else "N/A", inline=True)
            embed.add_field(name="Target", value=f"${card.target_price:.2f}" if card.target_price else "N/A", inline=True)
            embed.add_field(name="Proposal ID", value=f"`{card.proposal_id}`", inline=False)
            embed.set_footer(text="Vesper Quant Engine • Click button to approve/reject")

            view = create_approval_view(card.proposal_id)

            if chart_bytes:
                file = discord.File(io.BytesIO(chart_bytes), filename=f"{card.ticker}_5m.png")
                embed.set_image(url=f"attachment://{card.ticker}_5m.png")
                msg = await channel.send(embed=embed, file=file, view=view)
            else:
                msg = await channel.send(embed=embed, view=view)

            logger.info(f"Sent Discord gateway proposal card for {card.proposal_id} (Msg ID: {msg.id})")
            return str(msg.id)
        except Exception as e:
            logger.error(f"Error sending Discord proposal card via gateway: {e}")
            return None


# Module-level gateway bot singleton
_active_gateway_bot: Optional[VesperDiscordBot] = None


def get_active_gateway_bot() -> Optional[VesperDiscordBot]:
    """Return active VesperDiscordBot instance if running and connected."""
    global _active_gateway_bot
    if _active_gateway_bot and _active_gateway_bot.is_ready():
        return _active_gateway_bot
    return None


async def run_discord_gateway_bot(bot_token: Optional[str] = None) -> None:
    """Run persistent Discord Gateway Bot until cancelled or stopped."""
    global _active_gateway_bot
    bot = VesperDiscordBot(bot_token=bot_token)
    if not bot.configured:
        logger.warning(
            "DISCORD_BOT_TOKEN not set (or still placeholder) -- Discord Gateway client "
            "will not start. Inbound button interactions over Discord gateway disabled."
        )
        return

    _active_gateway_bot = bot
    logger.info("Starting Vesper Discord Gateway Bot client (outbound gateway only)...")
    try:
        await bot.start(bot.bot_token)
    except Exception as e:
        logger.error(f"Discord Gateway Bot exited with error: {e}")
    finally:
        if not bot.is_closed():
            await bot.close()
        _active_gateway_bot = None
