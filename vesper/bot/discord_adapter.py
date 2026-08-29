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
    """Discord Webhook / Gateway Bot channel adapter."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
    ):
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")
        self.bot_token = bot_token or os.getenv("DISCORD_BOT_TOKEN", "")
        self.channel_id = channel_id or os.getenv("DISCORD_CHANNEL_ID", "")
        self._client = httpx.AsyncClient(timeout=10.0)

    @property
    def channel_name(self) -> str:
        return "discord"

    @property
    def gateway_configured(self) -> bool:
        return bool(self.bot_token and self.channel_id and not self.bot_token.startswith("your_"))

    @property
    def webhook_configured(self) -> bool:
        return bool(self.webhook_url and not self.webhook_url.startswith("your_"))

    @property
    def configured(self) -> bool:
        return self.gateway_configured or self.webhook_configured

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

        # Approval-card enrichment fields -- each added only when the card
        # actually has the value (never a 0/placeholder for something that
        # couldn't be computed or wasn't fetched; see vesper/bot/base.py).
        if card.worst_case_notional is not None:
            impact = (
                f" ({card.buying_power_impact_pct:.1%} of BP)"
                if card.buying_power_impact_pct is not None else ""
            )
            embed["fields"].append({
                "name": "Worst-Case Notional",
                "value": f"${card.worst_case_notional:,.2f}{impact}",
                "inline": True,
            })
        if card.open_long_option_count_before is not None:
            embed["fields"].append({
                "name": "Open Long Options",
                "value": f"{card.open_long_option_count_before} → {card.open_long_option_count_after}",
                "inline": True,
            })
        if card.sector and card.sector_notional_before is not None:
            embed["fields"].append({
                "name": f"{card.sector} Sector Notional",
                "value": f"${card.sector_notional_before:,.2f} → ${card.sector_notional_after:,.2f}",
                "inline": True,
            })
        if card.thesis_source:
            embed["fields"].append({"name": "Thesis Source", "value": card.thesis_source, "inline": True})
        if card.proposal_digest:
            embed["fields"].append({
                "name": "Proposal Digest",
                "value": f"`{card.proposal_digest[:16]}…`",
                "inline": False,
            })

        # 1. Attempt 5-minute candlestick chart generation
        chart_bytes: Optional[bytes] = None
        if card.ticker:
            try:
                from mcp_server.charts import generate_chart
                chart_res = await generate_chart(
                    ticker=card.ticker,
                    period="1d",
                    interval="5m",
                    show_emas=True,
                )
                if isinstance(chart_res, dict) and chart_res.get("base64"):
                    import base64
                    chart_bytes = base64.b64decode(chart_res["base64"])
                elif isinstance(chart_res, dict) and chart_res.get("path") and os.path.exists(chart_res["path"]):
                    with open(chart_res["path"], "rb") as f:
                        chart_bytes = f.read()
            except Exception as e:
                logger.debug("5m chart generation for %s unavailable: %s", card.ticker, e)
                chart_bytes = None

        # 2. If Gateway Bot is actively connected in process, use its channel.send()
        if self.gateway_configured:
            from vesper.bot.discord_gateway import get_active_gateway_bot
            active_bot = get_active_gateway_bot()
            if active_bot:
                try:
                    msg_id = await active_bot.send_proposal_card(
                        channel_id=self.channel_id,
                        card=card,
                        chart_bytes=chart_bytes,
                    )
                    if msg_id:
                        return msg_id
                except Exception as e:
                    logger.warning(f"Gateway bot send_proposal_card failed: {e}. Falling back to REST/webhook.")

            # 3. Direct Discord Bot REST API with dynamic item components
            try:
                import json
                from vesper.bot.discord_gateway import get_approval_components
                components = get_approval_components(card.proposal_id)
                headers = {
                    "Authorization": f"Bot {self.bot_token}",
                }
                api_url = f"https://discord.com/api/v10/channels/{self.channel_id}/messages"

                if chart_bytes:
                    embed["image"] = {"url": f"attachment://{card.ticker}_5m.png"}
                    resp = await self._client.post(
                        api_url,
                        headers=headers,
                        data={"payload_json": json.dumps({"embeds": [embed], "components": components})},
                        files={"file": (f"{card.ticker}_5m.png", chart_bytes, "image/png")},
                    )
                else:
                    resp = await self._client.post(
                        api_url,
                        headers=headers,
                        json={"embeds": [embed], "components": components},
                    )

                if resp.status_code in [200, 201]:
                    data = resp.json()
                    msg_id = str(data["id"])
                    logger.info(f"Sent Discord interactive proposal card for {card.proposal_id} (Msg ID: {msg_id})")
                    return msg_id
                else:
                    logger.warning(f"Discord Bot REST message failed ({resp.status_code}): {resp.text}. Falling back to webhook.")
            except Exception as e:
                logger.warning(f"Discord Bot REST error: {e}. Falling back to webhook.")

        # 4. Fallback to Webhook URL if configured
        if self.webhook_configured:
            if chart_bytes:
                embed["image"] = {"url": f"attachment://{card.ticker}_5m.png"}
                try:
                    import json
                    resp = await self._client.post(
                        self.webhook_url,
                        data={"payload_json": json.dumps({"username": "Vesper Quant Bot", "embeds": [embed]})},
                        files={"file": (f"{card.ticker}_5m.png", chart_bytes, "image/png")},
                    )
                    if resp.status_code in [200, 204]:
                        logger.info(f"Sent Discord proposal card with chart for {card.proposal_id}")
                        return card.proposal_id
                    logger.warning(f"Discord multipart upload failed with {resp.status_code}. Falling back to standard embed.")
                except Exception as e:
                    logger.warning(f"Discord multipart error: {e}. Falling back to standard embed.")

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
