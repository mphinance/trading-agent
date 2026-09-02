"""Tests for M8-03: Gamma levels in watch_setup.

Verifies:
1. Module imports core.td.levels() and never the raw ticker payload (get_gex_ticker).
2. Fixture levels() response where two flip sources straddle spot surfaces flip_split=true unchanged.
3. Serialized gamma section stays under 1.5KB.
4. No summary sentence uses target-implying language ('will move to', 'targeting', etc.).
"""

import ast
import json
from pathlib import Path
import pytest

from trading_mcp.gamma_summary import format_gamma_for_voice, get_compact_gamma, FORBIDDEN_TARGET_WORDS


def test_gamma_summary_imports_levels_and_never_raw_ticker():
    """Verify AST of trading_mcp.gamma_summary: imports core.td and never get_gex_ticker."""
    import trading_mcp.gamma_summary as mod

    source_path = Path(mod.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    # Assert no import of get_gex_ticker
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "get_gex_ticker" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            assert "get_gex_ticker" not in (node.module or "")
            for alias in node.names:
                assert alias.name != "get_gex_ticker"

    # Confirm core.td is imported
    imported_from_core_td = any(
        isinstance(node, ast.ImportFrom) and node.module == "core.td"
        for node in ast.walk(tree)
    )
    assert imported_from_core_td, "gamma_summary must import from core.td"


def test_gamma_summary_surfaces_flip_split_when_straddling_spot():
    """Construct a fixture levels() response where flip sources straddle spot.
    Assert flip_split=True reaches the output unchanged.
    """
    # Apex flip is 505.0, GEX flip is 495.0, spot is 500.0 (straddling)
    fixture_levels = {
        "symbol": "SPY",
        "spot": 500.0,
        "regime": "positive_gamma",
        "price_action": "mean_reverting",
        "flip": 505.0,
        "flip_source": "apex",
        "flip_gex": 495.0,
        "flip_apex": 505.0,
        "flip_split": True,
        "pin": 500.0,
        "net_gex": 1250000.0,
        "pc_gex_ratio": 0.85,
        "above_flip": False,
        "walls": [
            {"strike": 490.0, "side": "below", "net_gex": -500000.0},
            {"strike": 495.0, "side": "below", "net_gex": -200000.0},
            {"strike": 505.0, "side": "above", "net_gex": 800000.0},
            {"strike": 510.0, "side": "above", "net_gex": 1200000.0},
        ],
        "expirations": ["2026-09-04"],
        "as_of": "2026-09-02T18:00:00Z",
    }

    result = format_gamma_for_voice(fixture_levels)

    assert result["available"] is True
    assert result["flip_split"] is True
    assert result["flip_apex"] == 505.0
    assert result["flip_gex"] == 495.0
    assert "split" in result["summary_phrase"].lower()
    assert "505" in result["summary_phrase"]
    assert "495" in result["summary_phrase"]


def test_gamma_summary_stays_under_1_5kb():
    """Assert the serialized gamma section stays under 1.5KB."""
    fixture_levels = {
        "symbol": "NVDA",
        "spot": 125.50,
        "regime": "positive_gamma",
        "price_action": "pinning",
        "flip": 120.0,
        "flip_source": "gex",
        "flip_gex": 120.0,
        "flip_apex": None,
        "flip_split": False,
        "pin": 125.0,
        "net_gex": 850000.0,
        "pc_gex_ratio": 0.65,
        "above_flip": True,
        "walls": [
            {"strike": 115.0, "side": "below", "net_gex": -300000.0},
            {"strike": 120.0, "side": "below", "net_gex": -100000.0},
            {"strike": 130.0, "side": "above", "net_gex": 600000.0},
            {"strike": 135.0, "side": "above", "net_gex": 900000.0},
        ],
    }

    result = format_gamma_for_voice(fixture_levels)
    serialized = json.dumps(result)
    assert len(serialized) < 1536, f"Serialized gamma exceeds 1.5KB: {len(serialized)} bytes"


def test_gamma_summary_forbids_target_language():
    """Verify that summary phrase uses hedging/positioning phrasing and never forecast/target language."""
    fixture_levels = {
        "symbol": "AAPL",
        "spot": 220.0,
        "regime": "neutral",
        "flip": 215.0,
        "flip_split": False,
        "pin": 220.0,
        "above_flip": True,
    }

    result = format_gamma_for_voice(fixture_levels)
    phrase = result["summary_phrase"].lower()

    for forbidden in FORBIDDEN_TARGET_WORDS:
        assert forbidden not in phrase, f"Forbidden target phrase '{forbidden}' found in summary"
