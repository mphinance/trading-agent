"""Telegram Bot Adapter for Interactive Trade Approvals."""

from __future__ import annotations

import os
import logging
from typing import Optional
import httpx

from vesper.bot.base import ApprovalChannel, ProposalCard
from vesper.state import ExecutionResult

logger = logging.getLogger(__name__)


class TelegramAdapter(ApprovalChannel):
    """Telegram Bot channel adapter."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self._client = httpx.AsyncClient(timeout=10.0)

    @property
    def channel_name(self) -> str:
        return "telegram"

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id and not self.bot_token.startswith("your_"))

    @property
    def _api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    async def send_proposal_card(self, card: ProposalCard) -> Optional[str]:
        """Send proposal card with inline [Approve] and [Reject] buttons and 5m chart."""
        if not self.configured:
            logger.debug("Telegram not configured. Skipping proposal card.")
            return None

        text = card.format_text()
        # Inline keyboard markup
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ APPROVE & EXECUTE", "callback_data": f"approve:{card.proposal_id}"},
                    {"text": "❌ REJECT", "callback_data": f"reject:{card.proposal_id}"},
                ]
            ]
        }

        # 1. Attempt 5-minute candlestick chart generation (candlestick + EMA 8/21/34/55/89)
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

        # 2. Send via Telegram sendPhoto API (multipart photo + caption + inline buttons)
        if chart_bytes:
            try:
                import json
                resp = await self._client.post(
                    f"{self._api_url}/sendPhoto",
                    data={
                        "chat_id": self.chat_id,
                        "caption": text,
                        "parse_mode": "Markdown",
                        "reply_markup": json.dumps(reply_markup),
                    },
                    files={
                        "photo": (f"{card.ticker}_5m.png", chart_bytes, "image/png"),
                    },
                )
                data = resp.json()
                if resp.status_code == 200 and data.get("ok"):
                    msg_id = str(data["result"]["message_id"])
                    logger.info(f"Sent Telegram proposal photo card for {card.proposal_id} (Message ID: {msg_id})")
                    return msg_id
                logger.warning(f"Telegram sendPhoto failed ({resp.status_code}): {data}. Falling back to sendMessage.")
            except Exception as e:
                logger.warning(f"Telegram sendPhoto error: {e}. Falling back to sendMessage.")

        # 3. Fallback to text-only sendMessage API
        try:
            resp = await self._client.post(
                f"{self._api_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "reply_markup": reply_markup,
                },
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                msg_id = str(data["result"]["message_id"])
                logger.info(f"Sent Telegram proposal card for {card.proposal_id} (Message ID: {msg_id})")
                return msg_id
            logger.error(f"Telegram sendMessage failed: {data}")
            return None
        except Exception as e:
            logger.error(f"Telegram send_proposal_card error: {e}")
            return None

    async def send_execution_result(self, result: ExecutionResult) -> bool:
        if not self.configured:
            return False

        status_emoji = "🟢" if result.status in ["SUBMITTED", "DRY_RUN_SIMULATED"] else "🔴"
        text = (
            f"{status_emoji} **VESPER EXECUTION REPORT**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Proposal ID**: `{result.order_proposal_id}`\n"
            f"**Ticker**: `{result.ticker}`\n"
            f"**Status**: `{result.status}`\n"
            f"**Message**: {result.message}\n"
            f"**Time**: `{result.timestamp}`"
        )
        try:
            resp = await self._client.post(
                f"{self._api_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
            return resp.status_code == 200 and resp.json().get("ok", False)
        except Exception as e:
            logger.error(f"Telegram send_execution_result error: {e}")
            return False

    async def send_alert(self, title: str, message: str, level: str = "INFO") -> bool:
        if not self.configured:
            return False
        level_icon = "⚠️" if level == "WARNING" else ("🚨" if level == "ERROR" else "ℹ️")
        text = f"{level_icon} **{title}**\n\n{message}"
        try:
            resp = await self._client.post(
                f"{self._api_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
            return resp.status_code == 200 and resp.json().get("ok", False)
        except Exception as e:
            logger.error(f"Telegram send_alert error: {e}")
            return False
