"""Tests for TraderLady and persona parameter wiring."""

from __future__ import annotations

import pytest
from vesper.state import TradingState


def test_persona_in_trading_state():
    """Verify persona is accepted as a valid field in TradingState."""
    state: TradingState = {
        "session_id": "sess-persona-test",
        "mode": "dry_run",
        "selected_playbook": "all",
        "target_ticker": "SPY",
        "persona": "traderlady",
        "regime": None,
        "candidates": [],
        "technicals": {},
        "options_audits": {},
        "proposals": [],
        "rejected_proposals": [],
        "execution_results": [],
        "needs_human_approval": False,
        "human_decision": None,
        "audit_trail": [],
        "reflection_notes": [],
        "errors": [],
    }

    assert state["persona"] == "traderlady"
