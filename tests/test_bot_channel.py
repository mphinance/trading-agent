"""Unit tests for Channel-Agnostic Bot Engine (Module 2)."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from vesper.bot.base import ProposalCard, ApprovalChannel
from vesper.bot.telegram_adapter import TelegramAdapter
from vesper.bot.discord_adapter import DiscordAdapter
from vesper.bot.webhook_adapter import WebhookAdapter
from vesper.bot.manager import ChannelManager
from vesper.state import OrderProposal, ExecutionResult


def sample_proposal(asset_type="EQUITY"):
    return OrderProposal(
        id="prop-test-01",
        ticker="NVDA",
        side="BUY",
        order_type="LIMIT",
        limit_price=125.50,
        quantity=10,
        asset_type=asset_type,
        stop_loss=120.00,
        profit_target=135.00,
        max_risk=55.00,
        estimated_cost=1255.0 if asset_type == "EQUITY" else 125500.0,
    )


def test_proposal_card_formatting_equity():
    prop = sample_proposal("EQUITY")
    card = ProposalCard.from_proposal(prop, thesis="VCP breakout with heavy whale accumulation")
    assert card.est_cost == 1255.0
    text = card.format_text()
    assert "NVDA" in text
    assert "BUY" in text
    assert "$125.50" in text
    assert "VCP breakout" in text


def test_proposal_card_formatting_option():
    prop = sample_proposal("OPTION")
    card = ProposalCard.from_proposal(prop)
    assert card.est_cost == 125500.0  # x100 multiplier


def test_telegram_configured_flag():
    unconfigured = TelegramAdapter(bot_token="", chat_id="")
    assert not unconfigured.configured

    configured = TelegramAdapter(bot_token="12345:ABCDE", chat_id="999888")
    assert configured.configured


@pytest.mark.asyncio
async def test_telegram_send_proposal():
    """Verify text-only Telegram sendMessage when chart is not available."""
    adapter = TelegramAdapter(bot_token="12345:ABCDE", chat_id="999888")
    card = ProposalCard.from_proposal(sample_proposal())

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "result": {"message_id": 42}}

    with patch("mcp_server.charts.generate_chart", side_effect=Exception("Chart generation offline")):
        with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            msg_id = await adapter.send_proposal_card(card)
            assert msg_id == "42"
            mock_post.assert_called_once()
            url_called = mock_post.call_args[0][0]
            assert "sendMessage" in url_called


@pytest.mark.asyncio
async def test_telegram_send_proposal_with_5m_chart():
    """Verify Telegram sendPhoto multipart upload when 5m chart is generated."""
    adapter = TelegramAdapter(bot_token="12345:ABCDE", chat_id="999888")
    card = ProposalCard.from_proposal(sample_proposal())

    mock_chart_res = {
        "ticker": "NVDA",
        "base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY44YAAAAASUVORK5CYII=",
        "path": "/tmp/nvda_5m.png",
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "result": {"message_id": 777}}

    with patch("mcp_server.charts.generate_chart", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = mock_chart_res
        with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            msg_id = await adapter.send_proposal_card(card)
            assert msg_id == "777"
            mock_post.assert_called_once()
            url_called = mock_post.call_args[0][0]
            assert "sendPhoto" in url_called
            kwargs = mock_post.call_args.kwargs
            assert "files" in kwargs
            assert "photo" in kwargs["files"]


@pytest.mark.asyncio
async def test_telegram_send_proposal_sendphoto_failure_falls_back_to_text():
    """Verify fallback to sendMessage if sendPhoto HTTP call fails."""
    adapter = TelegramAdapter(bot_token="12345:ABCDE", chat_id="999888")
    card = ProposalCard.from_proposal(sample_proposal())

    mock_chart_res = {
        "ticker": "NVDA",
        "base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY44YAAAAASUVORK5CYII=",
    }

    mock_photo_fail = MagicMock()
    mock_photo_fail.status_code = 400
    mock_photo_fail.json.return_value = {"ok": False, "description": "PHOTO_INVALID"}

    mock_msg_ok = MagicMock()
    mock_msg_ok.status_code = 200
    mock_msg_ok.json.return_value = {"ok": True, "result": {"message_id": 888}}

    with patch("mcp_server.charts.generate_chart", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = mock_chart_res
        with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = [mock_photo_fail, mock_msg_ok]
            msg_id = await adapter.send_proposal_card(card)
            assert msg_id == "888"
            assert mock_post.call_count == 2
            assert "sendPhoto" in mock_post.call_args_list[0][0][0]
            assert "sendMessage" in mock_post.call_args_list[1][0][0]


def test_discord_configured_flag():
    unconfigured = DiscordAdapter(webhook_url="")
    assert not unconfigured.configured

    configured = DiscordAdapter(webhook_url="https://discord.com/api/webhooks/123/abc")
    assert configured.configured


@pytest.mark.asyncio
async def test_discord_send_proposal():
    """Verify Discord proposal card sending without chart."""
    adapter = DiscordAdapter(webhook_url="https://discord.com/api/webhooks/123/abc")
    card = ProposalCard.from_proposal(sample_proposal())

    mock_resp = MagicMock()
    mock_resp.status_code = 204

    with patch("mcp_server.charts.generate_chart", side_effect=Exception("Chart generation offline")):
        with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            res = await adapter.send_proposal_card(card)
            assert res == card.proposal_id
            mock_post.assert_called_once()
            assert "json" in mock_post.call_args.kwargs


@pytest.mark.asyncio
async def test_discord_send_proposal_with_5m_chart():
    """Verify Discord proposal card sending with multipart 5m chart upload."""
    adapter = DiscordAdapter(webhook_url="https://discord.com/api/webhooks/123/abc")
    card = ProposalCard.from_proposal(sample_proposal())

    mock_chart_res = {
        "ticker": "NVDA",
        "base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY44YAAAAASUVORK5CYII=",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 204

    with patch("mcp_server.charts.generate_chart", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = mock_chart_res
        with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            res = await adapter.send_proposal_card(card)
            assert res == card.proposal_id
            mock_post.assert_called_once()
            kwargs = mock_post.call_args.kwargs
            assert "files" in kwargs
            assert "file" in kwargs["files"]


@pytest.mark.asyncio
async def test_webhook_send_execution():
    adapter = WebhookAdapter(endpoint_url="https://example.com/webhook")
    result = ExecutionResult(
        order_proposal_id="prop-01",
        ticker="SPY",
        status="SUBMITTED",
        client_order_id="wb-01",
        filled_quantity=5,
        filled_price=580.0,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        ok = await adapter.send_execution_result(result)
        assert ok is True
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_channel_manager_multiplexing():
    t_adapter = TelegramAdapter(bot_token="t_tok", chat_id="t_chat")
    d_adapter = DiscordAdapter(webhook_url="https://discord.com/webhook")
    w_adapter = WebhookAdapter(endpoint_url="")  # unconfigured

    mgr = ChannelManager(channels=[t_adapter, d_adapter, w_adapter])
    assert len(mgr.active_channels) == 2

    with patch.object(t_adapter, "send_proposal_card", new_callable=AsyncMock) as mock_t, \
         patch.object(d_adapter, "send_proposal_card", new_callable=AsyncMock) as mock_d:
        mock_t.return_value = "msg-123"
        mock_d.return_value = "embed-456"

        res = await mgr.broadcast_proposal(sample_proposal())
        assert res["telegram"] == "msg-123"
        assert res["discord"] == "embed-456"
        assert "webhook" not in res
