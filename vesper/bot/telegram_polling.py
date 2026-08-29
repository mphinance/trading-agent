"""Telegram Long-Polling Loop for Inbound Approval Callbacks.

Proposal cards get sent to Telegram with inline Approve/Reject buttons
(`vesper/bot/telegram_adapter.py`), and `vesper/bot/inbound.py`'s
`ApprovalRegistry.handle_callback_payload()` already knows how to parse a
Telegram `callback_query` / `/halt` / `/resume` payload into a resolved
decision and resume the paused LangGraph thread. Until this module existed,
nothing ever called it with a real Telegram event -- tapping "Approve" on a
sent card did nothing (see ROADMAP.md Known Gaps #5).

Deliberately **long-polling** (`getUpdates`), not a webhook. A Telegram
webhook needs a public HTTPS endpoint Telegram's servers can POST to; this
process holds no authentication and can place trades, and CLAUDE.md rule 1
forbids binding anything but loopback/Tailscale for exactly that reason.
Long-polling only ever makes *outbound* HTTPS requests to
api.telegram.org -- no inbound port is opened, so the rule is never in
tension with this feature. Do not add a webhook path here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Telegram's long-poll wait. Its own docs recommend a large value (Telegram
# holds the HTTP connection open until an update arrives or this elapses) so
# we aren't hammering getUpdates every second.
DEFAULT_POLL_TIMEOUT = 30

# Backoff after a failed getUpdates call (network error, non-200, bad JSON).
# Deliberately short -- this is "don't hot-loop on a flaky connection," not
# a real retry/backoff policy.
DEFAULT_ERROR_BACKOFF = 5.0


class TelegramPoller:
    """Long-polls Telegram's `getUpdates` and routes updates to the ApprovalRegistry."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        poll_timeout: int = DEFAULT_POLL_TIMEOUT,
        error_backoff: float = DEFAULT_ERROR_BACKOFF,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.bot_token = bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.poll_timeout = poll_timeout
        self.error_backoff = error_backoff
        # The httpx client's own read timeout must exceed Telegram's
        # long-poll wait, or every call times out client-side before
        # Telegram ever gets a chance to respond.
        self._client = client or httpx.AsyncClient(timeout=poll_timeout + 10.0)
        self._offset: Optional[int] = None

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and not self.bot_token.startswith("your_"))

    @property
    def _api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    async def fetch_updates(self) -> List[Dict[str, Any]]:
        """One `getUpdates` call. Raises on network/HTTP/protocol failure."""
        params: Dict[str, Any] = {"timeout": self.poll_timeout}
        if self._offset is not None:
            params["offset"] = self._offset
        resp = await self._client.get(f"{self._api_url}/getUpdates", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates returned ok=false: {data}")
        return data.get("result", [])

    async def _answer_callback_query(self, callback_query_id: str) -> None:
        """Clear a tapped button's loading spinner. Best-effort, never raises."""
        try:
            await self._client.post(
                f"{self._api_url}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id},
            )
        except Exception as e:
            logger.debug(f"answerCallbackQuery failed (non-fatal): {e}")

    async def process_update(self, update: Dict[str, Any]) -> None:
        """Route one Telegram update to the ApprovalRegistry, if relevant.

        `handle_callback_payload()` already knows how to parse Telegram's
        raw update shape (`callback_query` for a button tap, or
        `message.text` for a `/halt` / `/resume` command) -- a Telegram
        `Update` object *is* the payload shape it expects, so it is handed
        through unmodified. Anything else (a plain chat message, an edited
        message, a different update type) is silently ignored here; the
        caller still advances the offset past it.
        """
        from vesper.bot.inbound import approval_registry

        has_callback = "callback_query" in update
        text = str((update.get("message") or {}).get("text", "")).strip()
        is_command = text.startswith("/halt") or text.startswith("/resume")

        if not has_callback and not is_command:
            return

        try:
            res = await approval_registry.handle_callback_payload(update)
            logger.info(f"Telegram update routed -> {res.get('status')} ({res.get('decision', res.get('message'))})")
        finally:
            if has_callback:
                cb_id = update["callback_query"].get("id")
                if cb_id:
                    await self._answer_callback_query(cb_id)

    async def poll_once(self) -> int:
        """Fetch and process a single batch of updates. Returns count fetched."""
        updates = await self.fetch_updates()
        for update in updates:
            update_id = update.get("update_id")
            try:
                await self.process_update(update)
            except Exception as e:
                logger.error(f"Error processing Telegram update {update_id}: {e}")
            finally:
                # Advance the offset even for updates we didn't act on, and
                # even if processing raised -- Telegram's semantics are that
                # anything at or below the last-acknowledged offset gets
                # redelivered on every subsequent call forever otherwise.
                if update_id is not None:
                    self._offset = update_id + 1
        return len(updates)

    async def run_forever(self) -> None:
        """Long-poll Telegram indefinitely, routing updates to the ApprovalRegistry.

        No-ops (logs and returns) if `TELEGRAM_BOT_TOKEN` is unset -- this is
        called from `vesper.py listen`'s startup path, and a missing token
        should not crash the process. A network error on a single
        `getUpdates` call is caught, logged, and backed off from; it never
        kills the loop.
        """
        if not self.configured:
            logger.warning(
                "TELEGRAM_BOT_TOKEN not set (or still a placeholder) -- Telegram "
                "long-polling will not start. Approve/Reject taps will not be received."
            )
            return

        logger.info("Telegram long-polling started (getUpdates only -- no inbound port opened).")
        while True:
            try:
                await self.poll_once()
            except httpx.HTTPError as e:
                logger.warning(f"Telegram getUpdates network/HTTP error: {e} -- backing off {self.error_backoff}s")
                await asyncio.sleep(self.error_backoff)
            except Exception as e:
                logger.error(f"Unexpected error in Telegram polling loop: {e} -- backing off {self.error_backoff}s")
                await asyncio.sleep(self.error_backoff)

    async def aclose(self) -> None:
        await self._client.aclose()


async def run_telegram_polling_loop(bot_token: Optional[str] = None) -> None:
    """Entry point used by `vesper.py listen`."""
    poller = TelegramPoller(bot_token=bot_token)
    try:
        await poller.run_forever()
    finally:
        await poller.aclose()
