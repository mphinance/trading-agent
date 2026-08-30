"""Telegram Bot Adapter for Interactive Trade Approvals."""

from __future__ import annotations

import os
import logging
from typing import Optional
import httpx

from vesper.bot.base import ApprovalChannel, ProposalCard
from vesper.state import ExecutionResult

logger = logging.getLogger(__name__)

# Telegram's sendPhoto `caption` has a materially lower length cap than a
# plain sendMessage `text` (~1024 vs ~4096 chars). The enriched card (worst-
# case notional, buying-power impact, before/after diff, thesis, digest) can
# get close to or past that once every optional line is populated -- rather
# than let Telegram silently truncate the caption, drop the chart and send
# the full text via sendMessage instead.
_TELEGRAM_CAPTION_LIMIT = 1024

# (period, interval, label) per panel, top to bottom. Daily first: it is the
# context you read before the entry timing, so it belongs above the 5m.
_CHART_PANELS = (
    ("6mo", "1d", "Daily — 6mo"),
    ("1d", "5m", "Intraday — 5m"),
)


async def _build_chart_image(ticker: str) -> Optional[bytes]:
    """Render the daily and 5m charts and stack them into one PNG.

    Degrades in steps rather than all-or-nothing, because a proposal card with
    one chart is far more useful than one with none:
      - both panels render  -> stacked image
      - one panel renders   -> that panel alone
      - neither renders     -> None, and the caller sends text-only

    A daily chart is generally available even when the intraday one is not
    (5m data is thin outside market hours, and empty for a symbol that has not
    traded today), so the common weekend/overnight case still gets context.
    """
    from mcp_server.charts import generate_chart

    pngs: list[bytes] = []
    for period, interval, label in _CHART_PANELS:
        try:
            res = await generate_chart(
                ticker=ticker, period=period, interval=interval, show_emas=True,
            )
            if isinstance(res, dict) and res.get("base64"):
                import base64
                pngs.append(base64.b64decode(res["base64"]))
            elif isinstance(res, dict) and res.get("path") and os.path.exists(res["path"]):
                with open(res["path"], "rb") as f:
                    pngs.append(f.read())
        except Exception as e:
            logger.debug("%s chart (%s/%s) unavailable for %s: %s",
                         label, period, interval, ticker, e)

    if not pngs:
        return None
    if len(pngs) == 1:
        return pngs[0]

    try:
        import io
        from PIL import Image

        imgs = [Image.open(io.BytesIO(p)).convert("RGB") for p in pngs]
        # Normalise to a common width so a size mismatch between the two
        # renders can't produce a lopsided composite.
        width = min(i.width for i in imgs)
        scaled = [
            i if i.width == width
            else i.resize((width, max(1, round(i.height * width / i.width))), Image.LANCZOS)
            for i in imgs
        ]
        canvas = Image.new("RGB", (width, sum(i.height for i in scaled)), (0, 0, 0))
        y = 0
        for i in scaled:
            canvas.paste(i, (0, y))
            y += i.height
        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as e:
        # Compositing is a nicety; never lose the chart over it.
        logger.debug("chart compositing failed for %s, sending the first panel alone: %s", ticker, e)
        return pngs[0]


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

        # 1. Attempt chart generation: daily for trend context, 5m for entry
        # timing, stacked into ONE image.
        #
        # Why composite instead of sending two photos: Telegram's
        # sendMediaGroup (the multi-photo endpoint) does NOT support
        # reply_markup, so an album would silently cost the approve/reject
        # buttons -- the entire point of the card. sendPhoto takes exactly one
        # image, so the two timeframes are stacked vertically into a single
        # PNG and the buttons survive.
        chart_bytes: Optional[bytes] = None
        if card.ticker:
            chart_bytes = await _build_chart_image(card.ticker)

        if chart_bytes and len(text) > _TELEGRAM_CAPTION_LIMIT:
            logger.info(
                "Proposal card for %s is %d chars, over Telegram's sendPhoto caption cap "
                "(%d) -- sending as sendMessage without the chart rather than letting "
                "Telegram truncate the caption.",
                card.proposal_id, len(text), _TELEGRAM_CAPTION_LIMIT,
            )
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
                        "photo": (f"{card.ticker}_daily_5m.png", chart_bytes, "image/png"),
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
