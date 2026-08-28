"""Tests for Module 2: Inbound Approval Callback & Bot Integration."""

from __future__ import annotations

import pytest
from vesper.bot.inbound import ApprovalRegistry
from vesper.halt import is_halted, resume
from vesper.state import OrderProposal, TradingState
from vesper.nodes.human_gate import human_gate_node


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


@pytest.mark.asyncio
async def test_telegram_callback_approval(clean_registry):
    """Verify Telegram inline button callback resolves pending proposal to APPROVE."""
    clean_registry.register_pending("prop-tg-1", "session-tg", details={"ticker": "NVDA"})
    assert len(clean_registry.list_pending()) == 1

    telegram_payload = {
        "callback_query": {
            "id": "cb-12345",
            "from": {"username": "michael_trader"},
            "data": "approve:prop-tg-1",
        }
    }

    res = await clean_registry.handle_callback_payload(telegram_payload)
    assert res["status"] == "RESOLVED"
    assert res["decision"] == "APPROVE"
    assert res["proposal_id"] == "prop-tg-1"

    decision_record = clean_registry.get_decision("prop-tg-1")
    assert decision_record is not None
    assert decision_record["decision"] == "APPROVE"
    assert decision_record["user_id"] == "michael_trader"


@pytest.mark.asyncio
async def test_discord_callback_rejection(clean_registry):
    """Verify Discord interaction component resolves pending proposal to REJECT."""
    clean_registry.register_pending("prop-dc-2", "session-dc", details={"ticker": "TSLA"})

    discord_payload = {
        "type": 3,
        "data": {"custom_id": "reject:prop-dc-2"},
        "member": {"user": {"username": "quant_lead"}},
    }

    res = await clean_registry.handle_callback_payload(discord_payload)
    assert res["status"] == "RESOLVED"
    assert res["decision"] == "REJECT"

    decision_record = clean_registry.get_decision("prop-dc-2")
    assert decision_record["decision"] == "REJECT"


@pytest.mark.asyncio
async def test_telegram_halt_command(clean_registry):
    """Verify Telegram /halt command triggers emergency freeze."""
    assert not is_halted()[0]

    halt_payload = {
        "message": {
            "text": "/halt Circuit breaker activated from mobile",
            "from": {"username": "michael_trader"},
        }
    }

    res = await clean_registry.handle_callback_payload(halt_payload)
    assert res["status"] == "HALTED"
    assert is_halted()[0]

    resume_payload = {
        "message": {
            "text": "/resume",
            "from": {"username": "michael_trader"},
        }
    }
    res_resume = await clean_registry.handle_callback_payload(resume_payload)
    assert res_resume["status"] == "ACTIVE"
    assert not is_halted()[0]


@pytest.mark.asyncio
async def test_human_gate_node_consumes_pre_resolved_inbound_decision(clean_registry):
    """Verify human_gate_node applies inbound decision without requiring manual interrupt."""
    prop = OrderProposal(
        id="prop-inbound-3",
        ticker="AAPL",
        asset_type="EQUITY",
        side="BUY",
        limit_price=220.0,
        quantity=5,
    )

    # Pre-resolve via inbound callback
    await clean_registry.submit_decision("prop-inbound-3", "APPROVE", source="mobile_app", user_id="michael")

    state: TradingState = {
        "session_id": "sess-inbound-test",
        "mode": "live",
        "selected_playbook": "all",
        "target_ticker": None,
        "regime": None,
        "candidates": [],
        "technicals": {},
        "options_audits": {},
        "proposals": [prop],
        "rejected_proposals": [],
        "execution_results": [],
        "needs_human_approval": True,
        "human_decision": None,  # Will be discovered from registry
        "audit_trail": [],
        "reflection_notes": [],
        "errors": [],
    }

    out = await human_gate_node(state)
    assert out["human_decision"] == "APPROVE"
    assert prop.approved is True


def test_auth_verifications(monkeypatch):
    """Verify Telegram secret, Discord Ed25519 signature, and Bearer token auth guards."""
    from vesper.bot.inbound import (
        verify_telegram_webhook_secret,
        verify_discord_signature,
        verify_bearer_token,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # 1. Telegram Secret
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "super-secret-tg-token")
    assert verify_telegram_webhook_secret("super-secret-tg-token") is True
    assert verify_telegram_webhook_secret("wrong-token") is False

    # 2. Bearer Token
    monkeypatch.setenv("VESPER_WEBHOOK_SECRET", "bearer-secret-xyz")
    assert verify_bearer_token("Bearer bearer-secret-xyz") is True
    assert verify_bearer_token("Bearer wrong") is False
    assert verify_bearer_token("Basic 123") is False

    # 3. Discord Ed25519 Cryptographic Verification
    priv_key = Ed25519PrivateKey.generate()
    pub_key = priv_key.public_key()
    pub_hex = pub_key.public_bytes_raw().hex()

    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    timestamp = "1724889600"
    body = b'{"type": 1}'
    message_to_sign = timestamp.encode("utf-8") + body
    sig_hex = priv_key.sign(message_to_sign).hex()

    assert verify_discord_signature(sig_hex, timestamp, body) is True
    assert verify_discord_signature("bad_sig", timestamp, body) is False
    assert verify_discord_signature(sig_hex, "diff_timestamp", body) is False


@pytest.mark.asyncio
async def test_inbound_aiohttp_server_endpoints(clean_registry, monkeypatch):
    """Verify inbound aiohttp web endpoints route callbacks and enforce auth."""
    from aiohttp.test_utils import TestClient, TestServer
    from vesper.bot.inbound import create_inbound_app

    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "tg-sec-123")
    monkeypatch.setenv("VESPER_WEBHOOK_SECRET", "token-456")

    app = create_inbound_app()
    assert app is not None

    async with TestClient(TestServer(app)) as client:
        # 1. Health Endpoint
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

        # 2. Telegram Webhook (Unauthorized)
        resp_tg_unauth = await client.post("/webhook/telegram", json={"foo": "bar"})
        assert resp_tg_unauth.status == 401

        # 3. Telegram Webhook (Authorized callback)
        clean_registry.register_pending("prop-http-tg", "sess-1")
        resp_tg_auth = await client.post(
            "/webhook/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "tg-sec-123"},
            json={
                "callback_query": {
                    "data": "approve:prop-http-tg",
                    "from": {"username": "admin"},
                }
            },
        )
        assert resp_tg_auth.status == 200
        res_data = await resp_tg_auth.json()
        assert res_data["decision"] == "APPROVE"
        assert clean_registry.get_decision("prop-http-tg")["decision"] == "APPROVE"

        # 4. REST Approval Webhook (Authorized)
        clean_registry.register_pending("prop-http-rest", "sess-2")
        resp_rest = await client.post(
            "/webhook/approval",
            headers={"Authorization": "Bearer token-456"},
            json={"proposal_id": "prop-http-rest", "decision": "REJECT"},
        )
        assert resp_rest.status == 200
        rest_data = await resp_rest.json()
        assert rest_data["decision"] == "REJECT"

