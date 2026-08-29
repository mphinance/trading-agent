"""Unit tests for Discord Gateway Bot & DynamicItem Approval Button Routing (Module 2)."""

from __future__ import annotations

import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord

from vesper.bot.base import ProposalCard
from vesper.bot.inbound import ApprovalRegistry
from vesper.bot.discord_gateway import (
    ApprovalButton,
    APPROVAL_REGEX,
    create_approval_view,
    get_approval_components,
    VesperDiscordBot,
    run_discord_gateway_bot,
)
from vesper.bot.discord_adapter import DiscordAdapter
from vesper.state import OrderProposal


@pytest.fixture
def clean_registry(tmp_path, monkeypatch):
    """Isolate registry and halt state."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("vesper.halt._DATA_DIR", data_dir)
    monkeypatch.setattr("vesper.halt._HALT_STATE_PATH", data_dir / "halt_state.json")

    registry = ApprovalRegistry()
    monkeypatch.setattr("vesper.bot.inbound.approval_registry", registry)
    return registry


def test_approval_button_initialization_and_custom_id():
    """Verify ApprovalButton sets correct custom_id and styling."""
    approve_btn = ApprovalButton("approve", "prop-abc-123")
    assert approve_btn.action == "approve"
    assert approve_btn.proposal_id == "prop-abc-123"
    assert approve_btn.item.custom_id == "vesper|approve|prop-abc-123"
    assert approve_btn.item.style == discord.ButtonStyle.green

    reject_btn = ApprovalButton("reject", "prop-xyz-789")
    assert reject_btn.action == "reject"
    assert reject_btn.proposal_id == "prop-xyz-789"
    assert reject_btn.item.custom_id == "vesper|reject|prop-xyz-789"
    assert reject_btn.item.style == discord.ButtonStyle.red


@pytest.mark.asyncio
async def test_approval_button_from_custom_id_round_trip():
    """Verify from_custom_id reconstructs the button from regex match."""
    regex = re.compile(APPROVAL_REGEX)
    match = regex.fullmatch("vesper|approve|prop-test-456")
    assert match is not None

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_btn_item = MagicMock(spec=discord.ui.Button)

    reconstructed = await ApprovalButton.from_custom_id(mock_interaction, mock_btn_item, match)
    assert reconstructed.action == "approve"
    assert reconstructed.proposal_id == "prop-test-456"
    assert reconstructed.item.custom_id == "vesper|approve|prop-test-456"


@pytest.mark.asyncio
async def test_approval_button_callback_resolves_registry(clean_registry):
    """Verify clicking dynamic item button submits decision to ApprovalRegistry."""
    clean_registry.register_pending("prop-dc-click", "sess-dc")

    btn = ApprovalButton("approve", "prop-dc-click")

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = 998877
    mock_interaction.user.name = "quant_trader"
    mock_interaction.response.is_done.return_value = False
    mock_interaction.response.send_message = AsyncMock()

    await btn.callback(mock_interaction)

    # Verify decision recorded in registry
    decision = clean_registry.get_decision("prop-dc-click")
    assert decision is not None
    assert decision["decision"] == "APPROVE"
    assert decision["source"] == "discord"
    assert "quant_trader" in decision["user_id"]

    # Verify response sent back to Discord
    mock_interaction.response.send_message.assert_called_once()
    msg_sent = mock_interaction.response.send_message.call_args[0][0]
    assert "prop-dc-click" in msg_sent
    assert "APPROVED" in msg_sent


def test_create_approval_view_and_components():
    """Verify create_approval_view and get_approval_components builders."""
    view = create_approval_view("prop-view-1")
    assert len(view.children) == 2
    assert view.children[0].item.custom_id == "vesper|approve|prop-view-1"
    assert view.children[1].item.custom_id == "vesper|reject|prop-view-1"

    components = get_approval_components("prop-view-2")
    assert len(components) == 1
    assert components[0]["type"] == 1
    assert len(components[0]["components"]) == 2
    assert components[0]["components"][0]["custom_id"] == "vesper|approve|prop-view-2"
    assert components[0]["components"][1]["custom_id"] == "vesper|reject|prop-view-2"


@pytest.mark.asyncio
async def test_vesper_discord_bot_setup_and_messages(monkeypatch, clean_registry):
    """Verify VesperDiscordBot registers dynamic items and processes halt/resume."""
    from vesper.halt import is_halted

    bot = VesperDiscordBot(bot_token="test-bot-token")
    assert bot.configured is True

    # Test setup_hook
    with patch.object(bot, "add_dynamic_items") as mock_add:
        await bot.setup_hook()
        mock_add.assert_called_once_with(ApprovalButton)

    # Test /halt command message
    mock_msg_halt = MagicMock(spec=discord.Message)
    mock_msg_halt.author.bot = False
    mock_msg_halt.author.name = "admin"
    mock_msg_halt.author.id = 112233
    mock_msg_halt.content = "/halt Emergency test freeze"
    mock_msg_halt.channel.send = AsyncMock()

    await bot.on_message(mock_msg_halt)
    assert is_halted()[0] is True
    mock_msg_halt.channel.send.assert_called_once()
    assert "EMERGENCY FREEZE" in mock_msg_halt.channel.send.call_args[0][0]

    # Test /resume command message
    mock_msg_resume = MagicMock(spec=discord.Message)
    mock_msg_resume.author.bot = False
    mock_msg_resume.author.name = "admin"
    mock_msg_resume.author.id = 112233
    mock_msg_resume.content = "/resume"
    mock_msg_resume.channel.send = AsyncMock()

    await bot.on_message(mock_msg_resume)
    assert is_halted()[0] is False
    mock_msg_resume.channel.send.assert_called_once()
    assert "SYSTEM RESUMED" in mock_msg_resume.channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_discord_adapter_gateway_active_routing():
    """Verify DiscordAdapter routes through active Gateway bot client when connected."""
    adapter = DiscordAdapter(bot_token="bot-token-123", channel_id="123456789")
    assert adapter.gateway_configured is True

    prop = OrderProposal(
        id="prop-gw-test",
        ticker="QQQ",
        side="BUY",
        limit_price=480.0,
        quantity=5,
        estimated_cost=2400.0,
    )
    card = ProposalCard.from_proposal(prop)

    mock_bot = MagicMock()
    mock_bot.is_ready.return_value = True
    mock_bot.send_proposal_card = AsyncMock(return_value="msg-gw-999")

    with patch("vesper.bot.discord_gateway.get_active_gateway_bot", return_value=mock_bot):
        msg_id = await adapter.send_proposal_card(card)
        assert msg_id == "msg-gw-999"
        mock_bot.send_proposal_card.assert_called_once()


@pytest.mark.asyncio
async def test_discord_adapter_bot_rest_fallback():
    """Verify DiscordAdapter uses Discord REST API with components when bot client not in memory."""
    adapter = DiscordAdapter(bot_token="bot-token-123", channel_id="123456789")
    prop = OrderProposal(
        id="prop-rest-test",
        ticker="SPY",
        side="BUY",
        limit_price=560.0,
        quantity=2,
        estimated_cost=1120.0,
    )
    card = ProposalCard.from_proposal(prop)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "msg-rest-888"}

    with patch("vesper.bot.discord_gateway.get_active_gateway_bot", return_value=None):
        with patch("mcp_server.charts.generate_chart", side_effect=Exception("no chart")):
            with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_resp
                msg_id = await adapter.send_proposal_card(card)
                assert msg_id == "msg-rest-888"
                mock_post.assert_called_once()
                call_args, call_kwargs = mock_post.call_args
                assert "channels/123456789/messages" in call_args[0]
                assert "Bot bot-token-123" in call_kwargs["headers"]["Authorization"]
                assert "components" in call_kwargs["json"]


@pytest.mark.asyncio
async def test_run_discord_gateway_bot_unconfigured():
    """Verify run_discord_gateway_bot logs and returns cleanly when token missing."""
    with patch("vesper.bot.discord_gateway.os.getenv", return_value=""):
        await run_discord_gateway_bot(bot_token="")
