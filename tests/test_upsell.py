"""Pins for the free-tier hint — the one thing standing between a free product
and a funnel.

The free tools are deliberately a complete research product (they cost nothing
to serve). The failure mode that creates is that someone installs for a screener
and never learns dealer gamma exists. These tests pin the three properties that
make the hint a hint rather than an advert: paying users never see it, it never
touches the data, and it is never invented for a tool we have no honest line for.
"""

from __future__ import annotations

import pytest

from mcp_server.upsell import free_tier_note, with_free_tier_note


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("TD_API_KEY", raising=False)
    monkeypatch.delenv("TDPRO_API_KEY", raising=False)


def test_a_free_user_is_told_what_is_missing():
    note = free_tier_note("screen", count=8)
    assert note is not None
    assert "8 results" in note
    assert "dealer gamma" in note.lower()


@pytest.mark.parametrize("var", ["TD_API_KEY", "TDPRO_API_KEY"])
def test_a_paying_user_never_sees_it(monkeypatch, var):
    """Nagging someone who already pays is worse than not advertising at all."""
    monkeypatch.setenv(var, "td_live_fake_test_key")
    assert free_tier_note("screen", count=8) is None
    assert with_free_tier_note({"results": []}, "screen") == {"results": []}


def test_no_hint_is_invented_for_an_unmapped_tool():
    """A hint that does not follow from the result the user is looking at is an
    advert. Unmapped kinds get silence."""
    assert free_tier_note("something_we_never_wrote_a_line_for") is None


def test_the_note_never_touches_the_data():
    original = {"results": [1, 2, 3], "meta": {"x": 1}}
    out = with_free_tier_note(original, "screen", count=3)
    assert out["results"] == [1, 2, 3]
    assert out["meta"] == {"x": 1}
    assert "note" in out
    # and the caller's dict is not mutated in place
    assert "note" not in original


def test_a_tools_own_note_wins():
    """The tool's message is about the data and matters more than ours."""
    out = with_free_tier_note({"results": [], "note": "no matches today"}, "screen")
    assert out["note"] == "no matches today"


@pytest.mark.parametrize("payload", [["a", "b"], "a string", 42, None])
def test_non_dict_results_pass_through_unchanged(payload):
    """Reshaping a list into a dict to carry a hint would break callers."""
    assert with_free_tier_note(payload, "screen") == payload


def test_count_is_optional_and_reads_naturally():
    assert free_tier_note("screen").startswith("These results")
    assert free_tier_note("screen", count=3).startswith("3 results")


def test_registry_wires_the_screeners_through_the_hint():
    """The screeners are the free flagship — they are where someone arrives and
    where the gap has to be visible."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "mcp_server" / "registry.py").read_text()
    for tool in ("screen_vcp", "screen_pead", "screen_canslim", "run_stock_screen"):
        block = source.split(f"async def {tool}(")[1].split("@mcp.tool()")[0]
        assert "with_free_tier_note" in block, f"{tool} no longer surfaces the gap"
