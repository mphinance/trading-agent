"""Tests for Module 2: Inbound Approval Callback & Bot Integration."""

from __future__ import annotations

import pytest
from vesper.bot.inbound import ApprovalRegistry
from core.halt import is_halted, resume
from vesper.state import OrderProposal, TradingState
from vesper.nodes.human_gate import human_gate_node


@pytest.fixture
def clean_registry(tmp_path, monkeypatch):
    """Isolate registry and halt state."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("core.halt._DATA_DIR", data_dir)
    monkeypatch.setattr("core.halt._HALT_STATE_PATH", data_dir / "halt_state.json")

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


# ── Per-user authorization for Telegram callbacks/commands ──────────────────
# Regression tests for the same vulnerability class already fixed in
# vesper/bot/discord_gateway.py: the aiohttp webhook route's HMAC/secret
# checks (test_auth_verifications below) prove a request came from
# Telegram's own servers, but say nothing about WHICH Telegram user tapped
# approve/reject or typed /halt -- without a per-user allowlist, anyone who
# could message the bot could resolve any proposal or freeze/unfreeze trading.

@pytest.mark.asyncio
async def test_telegram_callback_rejects_unauthorized_user(clean_registry, monkeypatch):
    import core.approval_registry as core_approval
    monkeypatch.setattr(core_approval, "_AUTHORIZED_TELEGRAM_USER_IDS", {"111"})

    clean_registry.register_pending("prop-tg-unauth", "session-tg")
    payload = {
        "callback_query": {
            "id": "cb-unauth",
            "from": {"username": "stranger", "id": 999999},
            "data": "approve:prop-tg-unauth",
        }
    }

    res = await clean_registry.handle_callback_payload(payload)
    assert res["status"] == "UNAUTHORIZED"
    assert clean_registry.get_decision("prop-tg-unauth") is None


@pytest.mark.asyncio
async def test_telegram_callback_allows_authorized_user(clean_registry, monkeypatch):
    import core.approval_registry as core_approval
    monkeypatch.setattr(core_approval, "_AUTHORIZED_TELEGRAM_USER_IDS", {"111"})

    clean_registry.register_pending("prop-tg-auth-ok", "session-tg")
    payload = {
        "callback_query": {
            "id": "cb-auth-ok",
            "from": {"username": "michael_trader", "id": 111},
            "data": "approve:prop-tg-auth-ok",
        }
    }

    res = await clean_registry.handle_callback_payload(payload)
    assert res["status"] == "RESOLVED"
    assert clean_registry.get_decision("prop-tg-auth-ok")["decision"] == "APPROVE"


@pytest.mark.asyncio
async def test_telegram_halt_rejects_unauthorized_user(clean_registry, monkeypatch):
    import core.approval_registry as core_approval
    monkeypatch.setattr(core_approval, "_AUTHORIZED_TELEGRAM_USER_IDS", {"111"})

    assert not is_halted()[0]
    payload = {
        "message": {
            "text": "/halt Circuit breaker",
            "from": {"username": "stranger", "id": 999999},
        }
    }
    res = await clean_registry.handle_callback_payload(payload)
    assert res["status"] == "UNAUTHORIZED"
    assert not is_halted()[0]


@pytest.mark.asyncio
async def test_telegram_halt_allows_authorized_user(clean_registry, monkeypatch):
    import core.approval_registry as core_approval
    monkeypatch.setattr(core_approval, "_AUTHORIZED_TELEGRAM_USER_IDS", {"111"})

    payload = {
        "message": {
            "text": "/halt Circuit breaker",
            "from": {"username": "michael_trader", "id": 111},
        }
    }
    res = await clean_registry.handle_callback_payload(payload)
    assert res["status"] == "HALTED"
    assert is_halted()[0]
    resume(source="test-cleanup")


# ── Per-user authorization for Discord legacy webhook interactions ──────────

@pytest.mark.asyncio
async def test_discord_callback_rejects_unauthorized_user(clean_registry, monkeypatch):
    import core.approval_registry as core_approval
    monkeypatch.setattr(core_approval, "_AUTHORIZED_DISCORD_USER_IDS", {"55555"})

    clean_registry.register_pending("prop-dc-unauth", "session-dc")
    payload = {
        "type": 3,
        "data": {"custom_id": "approve:prop-dc-unauth"},
        "member": {"user": {"username": "stranger", "id": "99999"}},
    }

    res = await clean_registry.handle_callback_payload(payload)
    assert res["status"] == "UNAUTHORIZED"
    assert clean_registry.get_decision("prop-dc-unauth") is None


@pytest.mark.asyncio
async def test_discord_callback_allows_authorized_user(clean_registry, monkeypatch):
    import core.approval_registry as core_approval
    monkeypatch.setattr(core_approval, "_AUTHORIZED_DISCORD_USER_IDS", {"55555"})

    clean_registry.register_pending("prop-dc-auth-ok", "session-dc")
    payload = {
        "type": 3,
        "data": {"custom_id": "reject:prop-dc-auth-ok"},
        "member": {"user": {"username": "quant_lead", "id": "55555"}},
    }

    res = await clean_registry.handle_callback_payload(payload)
    assert res["status"] == "RESOLVED"
    assert clean_registry.get_decision("prop-dc-auth-ok")["decision"] == "REJECT"


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


def test_auth_guards_fail_closed_when_unconfigured(monkeypatch):
    """An unset secret must reject every request, not accept everything.

    A prior version of these guards returned True ("authorized") when the
    corresponding env var was unset, meaning a deploy that forgot to set
    TELEGRAM_WEBHOOK_SECRET/DISCORD_PUBLIC_KEY/VESPER_WEBHOOK_SECRET would
    silently accept unauthenticated approve/reject/halt commands from anyone
    who could reach the port. This is the regression test for that fix.
    """
    from vesper.bot.inbound import (
        verify_telegram_webhook_secret,
        verify_discord_signature,
        verify_bearer_token,
    )

    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("DISCORD_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VESPER_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("VESPER_API_TOKEN", raising=False)

    assert verify_telegram_webhook_secret("anything") is False
    assert verify_telegram_webhook_secret(None) is False
    assert verify_discord_signature("sig", "ts", b"{}") is False
    assert verify_bearer_token("Bearer anything") is False
    assert verify_bearer_token(None) is False


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



# ── Disk-backed persistence (survives a fresh ApprovalRegistry instance) ────
# The property this exists for: vesper loop is a long-lived daemon now, and
# a proposal can genuinely be sitting in a Telegram chat awaiting a tap when
# the process crashes or redeploys. An in-memory-only registry would
# silently strand it. These tests use TWO separate ApprovalRegistry()
# instances (not the same object with a different reference) to prove the
# state lives on disk, not in the Python object -- pytest's autouse
# _isolated_vesper_state fixture (conftest.py) points both instances at the
# SAME isolated tmp_path, exactly like two processes sharing one real
# vesper/data/ directory would.

def test_pending_proposal_survives_a_fresh_registry_instance():
    registry1 = ApprovalRegistry()
    registry1.register_pending("prop-persist-1", "sess-1", details={"ticker": "NVDA"})

    registry2 = ApprovalRegistry()  # simulates a fresh process
    pending = registry2.get_pending("prop-persist-1")
    assert pending is not None
    assert pending["session_id"] == "sess-1"
    assert pending["details"]["ticker"] == "NVDA"
    assert len(registry2.list_pending()) == 1


@pytest.mark.asyncio
async def test_decision_survives_a_fresh_registry_instance():
    registry1 = ApprovalRegistry()
    registry1.register_pending("prop-persist-2", "sess-2")
    await registry1.submit_decision("prop-persist-2", "APPROVE", source="telegram", user_id="michael")

    registry2 = ApprovalRegistry()
    decision = registry2.get_decision("prop-persist-2")
    assert decision is not None
    assert decision["decision"] == "APPROVE"
    assert decision["user_id"] == "michael"
    # Resolved proposals drop out of list_pending on ANY instance, since
    # status lives on disk, not on whichever object made the change.
    assert len(registry2.list_pending()) == 0


@pytest.mark.asyncio
async def test_graph_app_is_not_persisted_across_instances():
    """set_graph_app is deliberately per-instance, not disk-backed -- a
    freshly started process must call it again with its own newly-built
    graph object (see vesper/graph.py's persistent checkpointer for why
    that's still safe: the checkpoint state itself survives on disk even
    though this Python reference doesn't)."""
    registry1 = ApprovalRegistry()
    registry1.set_graph_app(object())

    registry2 = ApprovalRegistry()
    assert registry2._graph_app is None


# ── First decision wins (audit-integrity, not double-execution) ─────────────
# LangGraph itself is safe against a duplicate resume -- verified empirically:
# Command(resume=...) on an already-completed thread does not re-execute, so
# there is no double-order risk. What was NOT safe was the RECORD: a second
# decision used to overwrite the first, so a REJECT arriving after an APPROVE
# had already executed would rewrite the audit trail to contradict the broker.

@pytest.mark.asyncio
async def test_duplicate_decision_does_not_overwrite_the_first(clean_registry):
    clean_registry.register_pending("prop-dup", "sess-1")
    first = await clean_registry.submit_decision("prop-dup", "APPROVE", source="telegram", user_id="michael")
    assert first["status"] == "RESOLVED"

    # A REJECT lands afterwards -- redelivered callback, second authorised user,
    # or a double-tap. It must NOT rewrite what happened.
    second = await clean_registry.submit_decision("prop-dup", "REJECT", source="discord", user_id="someone_else")
    assert second["status"] == "ALREADY_RESOLVED"
    assert second["decision"] == "APPROVE", "must report the decision that actually stands"
    assert second["ignored"] == "REJECT"

    stored = clean_registry.get_decision("prop-dup")
    assert stored["decision"] == "APPROVE"
    assert stored["user_id"] == "michael", "the original resolver must survive"


@pytest.mark.asyncio
async def test_duplicate_identical_decision_is_also_refused(clean_registry):
    """Even a repeat of the SAME decision is refused rather than re-applied --
    re-applying would re-invoke the graph for an already-resolved thread."""
    clean_registry.register_pending("prop-dup2", "sess-1")
    await clean_registry.submit_decision("prop-dup2", "APPROVE", source="telegram", user_id="michael")
    again = await clean_registry.submit_decision("prop-dup2", "APPROVE", source="telegram", user_id="michael")
    assert again["status"] == "ALREADY_RESOLVED"
