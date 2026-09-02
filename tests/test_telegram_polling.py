"""Tests for the Telegram long-polling loop (vesper/bot/telegram_polling.py).

Follows the mocking style used in tests/test_bot_channel.py and
tests/test_inbound_bot.py: patch the httpx.AsyncClient methods on the
adapter/poller instance rather than hitting the network, and use a
clean ApprovalRegistry per test so pending/decision state doesn't leak
across tests.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from vesper.bot.inbound import ApprovalRegistry
from vesper.bot.telegram_polling import TelegramPoller


@pytest.fixture
def clean_registry(tmp_path, monkeypatch):
    """Isolate registry and halt state, same as tests/test_inbound_bot.py."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("core.halt._DATA_DIR", data_dir)
    monkeypatch.setattr("core.halt._HALT_STATE_PATH", data_dir / "halt_state.json")

    registry = ApprovalRegistry()
    monkeypatch.setattr("vesper.bot.inbound.approval_registry", registry)
    return registry


def _mock_get_updates_response(results):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"ok": True, "result": results}
    return resp


def test_configured_flag():
    unconfigured = TelegramPoller(bot_token="")
    assert not unconfigured.configured

    placeholder = TelegramPoller(bot_token="your_bot_token_here")
    assert not placeholder.configured

    configured = TelegramPoller(bot_token="12345:ABCDE")
    assert configured.configured


@pytest.mark.asyncio
async def test_missing_token_run_forever_noops(monkeypatch):
    """No TELEGRAM_BOT_TOKEN -- run_forever must log and return, never crash or hang."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    poller = TelegramPoller(bot_token="")

    with patch.object(poller, "poll_once", new_callable=AsyncMock) as mock_poll:
        await poller.run_forever()
        mock_poll.assert_not_called()


@pytest.mark.asyncio
async def test_offset_advances_across_calls():
    """The offset passed to getUpdates must be last_update_id + 1 on the next call."""
    poller = TelegramPoller(bot_token="12345:ABCDE")

    first_batch = [
        {"update_id": 100, "message": {"text": "hello", "from": {"username": "u"}}},
        {"update_id": 101, "message": {"text": "/halt", "from": {"username": "u"}}},
    ]

    with patch.object(poller._client, "get", new_callable=AsyncMock) as mock_get, \
         patch("core.halt.halt", return_value={"status": "HALTED", "message": "ok"}):
        mock_get.return_value = _mock_get_updates_response(first_batch)

        assert poller._offset is None
        await poller.poll_once()

        # offset must now be last update_id (101) + 1
        assert poller._offset == 102

        # Second call must pass the advanced offset as a param.
        mock_get.return_value = _mock_get_updates_response([])
        await poller.poll_once()
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["offset"] == 102


@pytest.mark.asyncio
async def test_offset_advances_even_for_unrelated_updates():
    """An update with no callback_query and no /halt //resume text must still advance offset."""
    poller = TelegramPoller(bot_token="12345:ABCDE")

    updates = [
        {"update_id": 5, "message": {"text": "just chatting", "from": {"username": "u"}}},
    ]

    with patch.object(poller._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get_updates_response(updates)
        count = await poller.poll_once()

    assert count == 1
    assert poller._offset == 6


@pytest.mark.asyncio
async def test_callback_query_routes_to_handle_callback_payload(clean_registry):
    """A button-tap callback_query update must reach ApprovalRegistry.handle_callback_payload
    with the exact shape it expects, resolving the pending proposal."""
    clean_registry.register_pending("prop-poll-1", "session-poll", details={"ticker": "NVDA"})

    poller = TelegramPoller(bot_token="12345:ABCDE")
    updates = [
        {
            "update_id": 200,
            "callback_query": {
                "id": "cbq-1",
                "from": {"username": "michael_trader"},
                "data": "approve:prop-poll-1",
            },
        }
    ]

    with patch.object(poller._client, "get", new_callable=AsyncMock) as mock_get, \
         patch.object(poller._client, "post", new_callable=AsyncMock) as mock_post:
        mock_get.return_value = _mock_get_updates_response(updates)
        mock_post.return_value = MagicMock(status_code=200)
        await poller.poll_once()

    decision = clean_registry.get_decision("prop-poll-1")
    assert decision is not None
    assert decision["decision"] == "APPROVE"
    assert decision["user_id"] == "michael_trader"

    # answerCallbackQuery should have been called to clear the button spinner.
    mock_post.assert_called_once()
    assert "answerCallbackQuery" in mock_post.call_args[0][0]

    # Offset must have advanced past this update regardless.
    assert poller._offset == 201


@pytest.mark.asyncio
async def test_slash_halt_command_routes_through(clean_registry):
    poller = TelegramPoller(bot_token="12345:ABCDE")
    updates = [
        {
            "update_id": 300,
            "message": {"text": "/halt from mobile", "from": {"username": "michael_trader"}},
        }
    ]

    with patch.object(poller._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get_updates_response(updates)
        await poller.poll_once()

    from core.halt import is_halted
    halted, _ = is_halted()
    assert halted


@pytest.mark.asyncio
async def test_network_error_does_not_kill_loop():
    """A getUpdates network error must be caught, logged, and backed off from --
    not propagate and kill the polling loop."""
    poller = TelegramPoller(bot_token="12345:ABCDE", error_backoff=0.0)

    call_count = {"n": 0}

    async def flaky_get(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("connection refused", request=MagicMock())
        # Second call succeeds with no updates, then we stop the loop by
        # raising a sentinel exception we catch outside run_forever.
        raise asyncio.CancelledError()

    with patch.object(poller._client, "get", side_effect=flaky_get):
        with pytest.raises(asyncio.CancelledError):
            await poller.run_forever()

    assert call_count["n"] == 2  # first call raised HTTPError and was caught+retried


@pytest.mark.asyncio
async def test_answer_callback_query_failure_is_non_fatal(clean_registry):
    """If answerCallbackQuery itself fails, the decision must still have been recorded."""
    clean_registry.register_pending("prop-poll-2", "session-poll-2", details={"ticker": "TSLA"})

    poller = TelegramPoller(bot_token="12345:ABCDE")
    updates = [
        {
            "update_id": 400,
            "callback_query": {
                "id": "cbq-2",
                "from": {"username": "michael_trader"},
                "data": "reject:prop-poll-2",
            },
        }
    ]

    with patch.object(poller._client, "get", new_callable=AsyncMock) as mock_get, \
         patch.object(poller._client, "post", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        mock_get.return_value = _mock_get_updates_response(updates)
        await poller.poll_once()

    decision = clean_registry.get_decision("prop-poll-2")
    assert decision is not None
    assert decision["decision"] == "REJECT"
