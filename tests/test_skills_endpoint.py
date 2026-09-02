"""Tests for Milestone 10: Skills endpoint, prompts, instructions, and rules.

M10-01 through M10-07.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import trading_mcp.prompts as prompts
import trading_mcp.resources as resources
import trading_mcp.server as server
from fastmcp import FastMCP


# ── M10-01: Server Instructions ───────────────────────────────────────────────

def test_m10_01_server_instructions_content_and_budget():
    """M10-01: instructions= is short, stays under 1500 chars, contains required

    substrings, and points to skill://rules without duplicating full text.
    """
    instr = server.SERVER_INSTRUCTIONS
    assert isinstance(instr, str)
    assert len(instr) < 1500, f"Instructions too long ({len(instr)} chars >= 1500)"

    # Required substrings
    assert "cannot increase exposure" in instr
    assert "halt" in instr
    assert "resume" in instr
    assert "copilot_setup" in instr
    assert "skill://rules" in instr

    # Does not dump full rules text (short pointer only)
    assert "NVDA" not in instr


# ── M10-02: copilot_setup Prompt ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_m10_02_copilot_setup_prompt():
    """M10-02: copilot_setup(proposal_id) scripts setup-watching session,

    names watch_setup, states cadence, enforces button rule, and handles unknown ids.
    """
    # 1. Direct function check
    text = prompts.copilot_setup_text("prop-sample-42")
    assert "watch_setup" in text
    assert "prop-sample-42" in text
    assert "cadence" in text.lower() or "second" in text.lower()
    assert "cannot approve" in text.lower() or "press it yourself" in text.lower()

    # Tells model to fetch thesis via watch_setup, not carry inline
    assert "fetch" in text.lower()

    # 2. Unknown proposal_id returns usable prompt without crashing
    unknown_text = prompts.copilot_setup_text("prop-unknown-999")
    assert "watch_setup" in unknown_text
    assert "prop-unknown-999" in unknown_text

    # 3. Via FastMCP prompt render
    m = FastMCP("test-prompts")
    prompts.register_prompts(m)
    p = await m.get_prompt("copilot_setup")
    assert p is not None
    rendered = await p.render(arguments={"proposal_id": "prop-sample-42"})
    msg_text = rendered.messages[0].content.text
    assert "watch_setup" in msg_text
    assert "prop-sample-42" in msg_text


# ── M10-03: morning_brief Prompt ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_m10_03_morning_brief_prompt():
    """M10-03: morning_brief() composes account state, gamma, pending setups, and alerts

    into a short spoken-friendly paragraph; handles degraded cases gracefully.
    """
    # 1. Normal rendered case with mocked data
    with patch("trading_mcp.vesper_tools.get_account_state") as mock_acct, \
         patch("core.td.TDPro.configured", True), \
         patch("core.td.TDPro.levels") as mock_levels, \
         patch("core.approval_registry.approval_registry.list_pending") as mock_pending, \
         patch("alerts.AlertStore.list") as mock_alerts:

        mock_acct.return_value = {
            "available": True,
            "net_liquidation": 12500.50,
            "position_count": 2,
        }
        mock_levels.return_value = {
            "spot": 510.5, "flip": 508.0
        }
        mock_pending.return_value = [
            {"proposal_id": "p1", "details": {"ticker": "NVDA"}},
            {"proposal_id": "p2", "details": {"ticker": "AAPL"}},
        ]
        mock_alerts.return_value = [
            {"alert_id": "a1", "symbol": "QQQ", "triggered": False}
        ]

        brief = prompts.morning_brief_text()
        assert isinstance(brief, str)
        assert "$12,500.50" in brief
        assert "2 open positions" in brief
        assert "SPY spot is 510.50" in brief
        assert "508.00" in brief
        assert "2 pending trade proposals" in brief
        assert "1 armed price alerts" in brief
        # Spoken friendly, not a raw JSON dump
        assert not brief.startswith("{")

    # 2. Degraded case: feeds fail or raise exceptions
    with patch("trading_mcp.vesper_tools.get_account_state", side_effect=Exception("down")), \
         patch("core.td.TDPro.levels", side_effect=Exception("down")), \
         patch("core.approval_registry.approval_registry.list_pending", side_effect=Exception("down")), \
         patch("alerts.AlertStore.list", side_effect=Exception("down")):

        degraded_brief = prompts.morning_brief_text()
        assert isinstance(degraded_brief, str)
        assert len(degraded_brief) > 20
        assert "Buttons move money" in degraded_brief or "approve" in degraded_brief


# ── M10-04: Skill Resources Discovery ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_m10_04_skill_resources_discovery_and_read():
    """M10-04: Every directory under skills/ is surfaced as skill://<name>,

    matches on-disk count (64 today), and sample reads match byte-for-byte.
    """
    m = FastMCP("test-skills")
    resources.register_skill_resources(m)

    # 1. Count check: 64 skills + 1 rules = 65 total
    disk_skills = resources.discover_skills()
    assert len(disk_skills) == 64, f"Expected 64 skills on disk, found {len(disk_skills)}"

    listed = await m.list_resources()
    listed_uris = {str(r.uri) for r in listed}
    assert "skill://rules" in listed_uris

    for s in disk_skills:
        assert f"skill://{s}" in listed_uris

    # 2. Byte-for-byte fidelity check for 3 sampled skills
    samples = ["0dte-flow", "momentum-squeeze", "canslim-screener"]
    for s in samples:
        expected_content = (resources.SKILLS_DIR / s / "SKILL.md").read_text(encoding="utf-8")
        read_res = await m.read_resource(f"skill://{s}")
        assert read_res.contents[0].content == expected_content

    # 3. Nonexistent skill returns clear not-found
    with pytest.raises(Exception):
        await m.read_resource("skill://nonexistent-ghost-skill")


# ── M10-05: Curated skill://rules ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_m10_05_curated_rules_resource():
    """M10-05: skill://rules contains the four required phrases/examples and stays bounded."""
    m = FastMCP("test-rules")
    resources.register_skill_resources(m)

    res = await m.read_resource("skill://rules")
    content = res.contents[0].content

    # Four required concepts/phrases:
    assert "gamma marks positioning not a forecast" in content.lower()
    assert "nvda" in content.lower() and "in video" in content.lower()
    assert "voice may do anything that cannot increase exposure" in content.lower()
    assert "buttons move money" in content.lower()

    # Bounded length (concise cheat sheet)
    assert len(content) < 3000


# ── M10-06: Path Traversal Safety & Exposure Pin ──────────────────────────────

@pytest.mark.asyncio
async def test_m10_06_path_traversal_safety():
    """M10-06: Reject path traversal attempts like skill://../../.env or skill://../CLAUDE.md."""
    # 1. Direct function rejection
    with pytest.raises(ValueError):
        resources.read_skill_content("../../.env")

    with pytest.raises(ValueError):
        resources.read_skill_content("../CLAUDE.md")

    # 2. MCP read_resource rejection
    m = FastMCP("test-traversal")
    resources.register_skill_resources(m)

    with pytest.raises(Exception):
        await m.read_resource("skill://../../.env")

    with pytest.raises(Exception):
        await m.read_resource("skill://../CLAUDE.md")


# ── M10-07: End-to-End Co-pilot Chain ────────────────────────────────────────

@pytest.mark.asyncio
async def test_m10_07_e2e_copilot_turn(tmp_path, monkeypatch):
    """M10-07: Client with zero local context runs a full co-pilot turn using

    only server-provided instructions, copilot_setup, skill://rules, and watch_setup.
    Second watch_setup call exhibits compression.
    """
    import core.approval_registry as reg
    state_file = tmp_path / "approval_registry_state.json"
    monkeypatch.setattr(reg, "_APPROVAL_STATE_PATH", state_file)
    registry = reg.ApprovalRegistry()
    monkeypatch.setattr(reg, "approval_registry", registry)

    import trading_mcp.voice_tools as vt
    monkeypatch.setattr(vt, "approval_registry", registry)

    # 1. Read instructions from server
    instr = server.SERVER_INSTRUCTIONS
    assert "copilot_setup" in instr
    assert "skill://rules" in instr

    # 2. Invoke copilot_setup prompt
    prop_id = "prop-e2e-nvda"
    registry.register_pending(
        proposal_id=prop_id,
        session_id="sess-e2e",
        details={"ticker": "NVDA", "side": "BUY", "limit_price": 120.0, "quantity": 10},
    )

    prompt_txt = prompts.copilot_setup_text(prop_id)
    assert "watch_setup" in prompt_txt
    assert prop_id in prompt_txt

    # 3. Read skill://rules resource
    rules_txt = resources.read_skill_content("rules")
    assert "buttons move money" in rules_txt.lower()

    # 4. Call watch_setup twice with mock market data
    with patch("core.wb.Webull") as mock_wb_cls, \
         patch("core.md.Market") as mock_md_cls, \
         patch("trading_mcp.voice_tools.get_compact_gamma") as mock_gamma:

        mock_wb = MagicMock()
        mock_wb_cls.return_value = mock_wb
        mock_md = MagicMock()
        mock_md_cls.return_value = mock_md
        mock_md.snapshot.return_value = {"NVDA": {"last": 118.0, "close": 115.0}}
        mock_gamma.return_value = {"summary_phrase": "Call wall 125.00, put wall 110.00"}

        # First call: full telemetry
        call1 = vt.watch_setup(prop_id)
        assert call1["available"] is True
        assert call1["symbol"] == "NVDA"
        assert call1.get("unchanged") is False

        # Second call (immediate): unchanged compression
        call2 = vt.watch_setup(prop_id)
        assert call2["available"] is True
        assert call2.get("unchanged") is True
        assert "unchanged" in call2.get("speakable_summary", "").lower()
