"""Tests for M8-02: summarize_bars_for_voice.

Verifies pure bar structure voice phrasing: consecutive higher/lower lows,
range direction, and volume vs 20-bar average. Zero chart/PNG/image dependencies.
"""

import sys
import pytest
from trading_mcp.bar_summary import summarize_bars_for_voice, BarSummary


def test_bar_summary_three_consecutive_higher_lows_and_half_volume():
    """Feed a synthetic series with three consecutive higher lows and half-average volume.
    Assert the phrase names both facts with correct numbers/ordinals.
    """
    # 20 baseline bars with volume 1000 and base price around 100
    bars = []
    for i in range(20):
        bars.append({
            "open": 100.0,
            "high": 105.0,
            "low": 98.0,
            "close": 101.0,
            "volume": 1000.0,
        })

    # Now append bars establishing consecutive higher lows leading to the final bar:
    # We want 3 consecutive higher lows at the end:
    # transition 1: bar[-3].low > bar[-4].low
    # transition 2: bar[-2].low > bar[-3].low
    # transition 3: bar[-1].low > bar[-2].low
    # bar[-4] low = 95.0
    # bar[-3] low = 96.0 (1st higher low)
    # bar[-2] low = 97.0 (2nd higher low)
    # bar[-1] low = 98.0 (3rd higher low), and volume = 500.0 (half of 1000 avg)
    bars.append({"open": 96.0, "high": 102.0, "low": 95.0, "close": 100.0, "volume": 1000.0})
    bars.append({"open": 97.0, "high": 103.0, "low": 96.0, "close": 101.0, "volume": 1000.0})
    bars.append({"open": 98.0, "high": 104.0, "low": 97.0, "close": 102.0, "volume": 1000.0})
    bars.append({"open": 99.0, "high": 105.0, "low": 98.0, "close": 103.0, "volume": 500.0})

    summary = summarize_bars_for_voice(bars)

    assert isinstance(summary, BarSummary)
    assert isinstance(summary, dict)
    phrase = summary.phrase
    assert "third consecutive higher low" in phrase.lower()
    assert "half the 20-bar average" in phrase.lower()

    # Numeric facts
    assert summary["consecutive_higher_lows"] == 3
    assert summary["volume_ratio"] == 0.5
    assert summary["current_low"] == 98.0
    assert summary["current_volume"] == 500.0

    # Ensure no raw bars list is returned in the payload
    assert "bars" not in summary
    assert "raw_bars" not in summary
    assert "ohlcv" not in summary


def test_bar_summary_no_higher_low_does_not_fabricate():
    """Feed a synthetic series with no higher-low pattern (e.g. descending lows).
    Assert it does NOT fabricate a higher-low phrase.
    """
    bars = []
    for i in range(25):
        # Steadily dropping lows
        bars.append({
            "open": 100.0 - i,
            "high": 102.0 - i,
            "low": 95.0 - i,
            "close": 96.0 - i,
            "volume": 1000.0,
        })

    summary = summarize_bars_for_voice(bars)
    phrase = summary.phrase

    assert "higher low" not in phrase.lower()
    assert summary["consecutive_higher_lows"] == 0
    assert summary["consecutive_lower_lows"] >= 2
    assert "lower low" in phrase.lower()


def test_bar_summary_flat_consolidation_no_fabrication():
    """Feed a series with flat identical lows and average volume."""
    bars = []
    for i in range(25):
        bars.append({
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000.0,
        })

    summary = summarize_bars_for_voice(bars)
    phrase = summary.phrase

    assert "higher low" not in phrase.lower()
    assert "lower low" not in phrase.lower()
    assert summary["consecutive_higher_lows"] == 0
    assert summary["consecutive_lower_lows"] == 0
    assert "volume in line with the 20-bar average" in phrase.lower()


def test_bar_summary_has_no_chart_png_dependencies():
    """Confirm the module has no matplotlib, PIL, or image/chart dependencies."""
    import ast
    from pathlib import Path
    import trading_mcp.bar_summary as mod

    source_path = Path(mod.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    # Verify no disallowed imports via AST
    disallowed = {"matplotlib", "PIL", "cv2", "seaborn", "plotly", "base64"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                assert root_pkg not in disallowed, f"Disallowed import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root_pkg = (node.module or "").split(".")[0]
            assert root_pkg not in disallowed, f"Disallowed import from: {node.module}"


def test_bar_summary_edge_cases():
    """Empty or single bar history degrades gracefully."""
    empty = summarize_bars_for_voice([])
    assert "no bar data available" in empty.phrase
    assert empty["consecutive_higher_lows"] == 0

    none_val = summarize_bars_for_voice(None)
    assert "no bar data available" in none_val.phrase

    single = summarize_bars_for_voice([{"low": 50, "high": 55, "close": 52, "volume": 100}])
    assert "1 bar available" in single.phrase
    assert single["consecutive_higher_lows"] == 0
