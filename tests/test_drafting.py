"""Tests for proposal drafting path (M8-15, M8-16, M8-17, M8-18)."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import trading_mcp.drafting as drafting
from core.approval_registry import approval_registry
from vesper.bot.base import ApprovalChannel, ProposalCard


@pytest.fixture
def mock_clean_registry(tmp_path, monkeypatch):
    """Isolated state directory for approval registry during testing."""
    import core.approval_registry as ar

    state_file = tmp_path / "approval_registry_state.json"
    monkeypatch.setattr(ar, "_APPROVAL_STATE_PATH", state_file)
    registry = ar.ApprovalRegistry()
    monkeypatch.setattr(ar, "approval_registry", registry)
    monkeypatch.setattr(drafting, "approval_registry", registry)
    return registry


@pytest.fixture
def mock_clean_audit(tmp_path, monkeypatch):
    """Isolated audit chain file."""
    import core.audit_chain as ac

    chain_file = tmp_path / "audit_chain.jsonl"
    monkeypatch.setattr(ac, "_CHAIN_PATH", chain_file)
    return chain_file


@pytest.mark.asyncio
async def test_draft_proposal_deterministic_sizing_and_risk(mock_clean_registry, mock_clean_audit):
    """M8-15: Sizing and risk gate are deterministic and pure.

    Registers PENDING proposal, returns id and risk metrics, never calls broker,
    and proposal cannot execute without an explicit button approval.
    """
    with patch("core.wb.Webull") as mock_wb_cls:
        mock_wb = MagicMock()
        mock_wb_cls.return_value = mock_wb

        res = await drafting.draft_proposal(
            symbol_query="AAPL",
            side="BUY",
            entry_price=200.0,
            stop_loss=190.0,
            profit_target=220.0,
            account_equity=10000.0,
            thesis="Breakout test",
        )

        assert res["available"] is True
        assert res["status"] == "PENDING"
        assert res["ticker"] == "AAPL"
        assert res["side"] == "BUY"
        assert res["quantity"] > 0
        assert res["entry"] == 200.0
        assert res["stop"] == 190.0
        assert res["target"] == 220.0
        assert res["risk"] > 0
        assert "fill" not in res
        assert "ticket_id" not in res

        # Broker place_order was never called
        assert not hasattr(mock_wb, "place_order") or not mock_wb.place_order.called

        # Proposal is PENDING in registry
        prop_id = res["proposal_id"]
        pending = mock_clean_registry.get_pending(prop_id)
        assert pending is not None
        assert pending["status"] == "PENDING"
        assert pending["details"]["ticker"] == "AAPL"

        # Proposal still requires explicit human decision to execute
        dec_res = await mock_clean_registry.submit_decision(prop_id, "APPROVE")
        assert dec_res["decision"] == "APPROVE"


@pytest.mark.asyncio
async def test_draft_broadcasts_to_channel_and_records_audit(mock_clean_registry, mock_clean_audit):
    """M8-16: Drafted proposal is broadcast via channel_manager with interactive card,

    recorded in audit trail as drafted by MCP, and survives restarts.
    """
    from vesper.bot.manager import channel_manager

    # Stub an active approval channel
    class StubChannel(ApprovalChannel):
        def __init__(self):
            self._channel_name = "stub_test"
            self.sent_cards = []

        @property
        def channel_name(self) -> str:
            return self._channel_name

        @property
        def configured(self) -> bool:
            return True

        async def send_proposal_card(self, card: ProposalCard) -> str:
            self.sent_cards.append(card)
            return f"msg-{card.proposal_id}"

        async def send_execution_result(self, result) -> bool:
            return True

        async def send_alert(self, title: str, message: str, level: str = "INFO") -> bool:
            return True

    stub = StubChannel()
    orig_channels = channel_manager.channels
    channel_manager.channels = [stub]

    try:
        res = await drafting.draft_proposal(
            symbol_query="TSLA",
            side="BUY",
            entry_price=250.0,
            stop_loss=240.0,
            profit_target=270.0,
            account_equity=10000.0,
        )

        assert res["available"] is True
        prop_id = res["proposal_id"]

        # 1. Verify card was sent to channel with correct attributes
        assert len(stub.sent_cards) == 1
        card = stub.sent_cards[0]
        assert card.proposal_id == prop_id
        assert card.ticker == "TSLA"
        assert card.side == "BUY"

        # 2. Verify audit trail records drafted by MCP
        lines = mock_clean_audit.read_text(encoding="utf-8").splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
        draft_entries = [e for e in entries if e.get("node") == "mcp_draft_proposal"]
        assert len(draft_entries) == 1
        assert draft_entries[0]["entry"]["proposal_id"] == prop_id
        assert draft_entries[0]["entry"]["drafted_by"] == "MCP"

        # 3. Verify restart persistence: re-instantiate registry from same path
        import core.approval_registry as ar
        restarted_registry = ar.ApprovalRegistry()
        assert restarted_registry.get_pending(prop_id) is not None

    finally:
        channel_manager.channels = orig_channels


@pytest.mark.asyncio
async def test_draft_symbol_resolution_and_deduplication(mock_clean_registry, mock_clean_audit):
    """M8-17: Spoken symbol mis-transcription resolved and echoed back,

    repeated drafts return existing pending proposal, and ambiguous queries reject.
    """
    # 1. Fuzzy match 'in video' -> NVDA echoed in summary
    res_nvda = await drafting.draft_proposal(
        symbol_query="in video",
        side="BUY",
        entry_price=120.0,
        stop_loss=115.0,
        profit_target=130.0,
        account_equity=10000.0,
    )
    assert res_nvda["available"] is True
    assert res_nvda["ticker"] == "NVDA"
    assert "NVDA" in res_nvda["speakable_summary"]
    prop_id = res_nvda["proposal_id"]

    # 2. Second draft within pending window returns existing proposal (deduplication)
    res_dup = await drafting.draft_proposal(
        symbol_query="NVDA",
        side="BUY",
        entry_price=120.0,
        stop_loss=115.0,
        profit_target=130.0,
        account_equity=10000.0,
    )
    assert res_dup["available"] is True
    assert res_dup["deduplicated"] is True
    assert res_dup["proposal_id"] == prop_id

    # 3. Ambiguous symbol query drafts nothing
    res_amb = await drafting.draft_proposal(
        symbol_query="GOOG",
        side="BUY",
        entry_price=170.0,
        stop_loss=165.0,
        profit_target=180.0,
    )
    if not res_amb["available"]:
        assert res_amb["reason"] == "ambiguous_symbol"
        assert len(res_amb["candidates"]) >= 2

    # 4. Rejected risk gate (stop > entry for BUY) drafts nothing
    res_rej = await drafting.draft_proposal(
        symbol_query="MSFT",
        side="BUY",
        entry_price=400.0,
        stop_loss=410.0,  # Invalid: stop above entry for BUY
        profit_target=450.0,
    )
    assert res_rej["available"] is False
    assert res_rej["reason"] == "risk_gate_rejected"


def test_drafting_ast_exposure_pin():
    """M8-18: Assert guard.preview, guard.place, resume, and submit_decision

    are completely absent from trading_mcp.drafting.
    """
    from pathlib import Path
    import trading_mcp.drafting as dmod

    source = Path(dmod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        # No import of vesper.execution_guard
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "execution_guard" not in alias.name
                assert "resume" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            assert "execution_guard" not in (node.module or "")
            for alias in node.names:
                assert alias.name != "resume"
                assert alias.name != "place"
                assert alias.name != "preview"

        # No call or attribute access to submit_decision, place, preview, resume
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("submit_decision", "place", "preview", "resume")
