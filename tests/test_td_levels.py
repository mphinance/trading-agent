"""td.levels() — merging two GEX tools into one compacted, honest structure."""

from __future__ import annotations

import json

import pytest

import td as T

GEX = {
    "symbol": "SPY", "spotPrice": 746.48, "totalGEX": 4.35e9,
    "gammaFlipLevel": 745.6, "maxGammaStrike": 743, "putCallGEXRatio": 0.6131,
    "interpretation": {"marketRegime": "Positive Gamma", "priceAction": "dampened"},
    "keyLevels": [{"strike": 743, "netGex": -1.38e9, "type": "support"},
                  {"strike": 746, "netGex": 1.23e9, "type": "pin"}],
    "byStrike": [
        {"strike": 635, "netGex": -14086.54, "callOi": 38, "putOi": 1113},   # far below
        {"strike": 733, "netGex": -1.12e8, "callOi": 2188, "putOi": 35908},
        {"strike": 743, "netGex": -1.38e9, "callOi": 14801, "putOi": 44972},
        {"strike": 746, "netGex": 1.23e9, "callOi": 14143, "putOi": 3180},
        {"strike": 750, "netGex": 7.45e8, "callOi": 42347, "putOi": 4924},
        {"strike": 770, "netGex": 0, "callOi": 9992, "putOi": 36},           # no gamma
        {"strike": 900, "netGex": 5.0e8, "callOi": 10, "putOi": 1},          # far above
    ],
    "expirationsUsed": ["2026-07-31", "2026-08-03"],
    "lastUpdated": "2026-07-31T20:00:01Z", "gammaPocket": None,
}
APEX = {"gammaFlip": 745.9, "levels": [
    {"strike": 743, "score": 100, "rank": 1, "totalOI": 59773, "isAboveSpot": False},
    {"strike": 750, "score": 81, "rank": 2, "totalOI": 47271, "isAboveSpot": True}]}


def client(gex=None, apex=APEX, apex_error=None):
    c = T.TDPro(api_key="test")

    def cached(tool, args=None, ttl=None):
        if tool == "get_gex_ticker":
            return gex if gex is not None else GEX
        if apex_error:
            raise T.TDProError(apex_error)
        return apex

    c.cached = cached
    return c


# --------------------------------------------------------------------------
# Compaction
# --------------------------------------------------------------------------

def test_compacted_payload_stays_small():
    """The raw ladder is ~40KB on SPY. It must never reach a chat turn."""
    out = client().levels("SPY")
    assert len(json.dumps(out)) < 4000


def test_far_strikes_and_zero_gamma_strikes_are_dropped():
    walls = client().levels("SPY")["walls"]
    strikes = [w["strike"] for w in walls]
    assert 635 not in strikes and 900 not in strikes, "outside the near-spot band"
    assert 770 not in strikes, "zero net gamma is not a wall"


def test_put_walls_survive_the_ranking():
    """Ranking by raw netGex would leave resistance above and no support below."""
    walls = client().levels("SPY")["walls"]
    assert any(w["net_gex"] < 0 for w in walls)
    assert any(w["net_gex"] > 0 for w in walls)


def test_walls_are_returned_in_ladder_order():
    walls = client().levels("SPY")["walls"]
    assert [w["strike"] for w in walls] == sorted(w["strike"] for w in walls)


def test_walls_are_labelled_relative_to_spot():
    for w in client().levels("SPY")["walls"]:
        assert w["side"] == ("above" if w["strike"] > 746.48 else "below")


def test_symbol_is_normalised():
    assert client().levels("spy")["symbol"] == "SPY"


# --------------------------------------------------------------------------
# The two flips disagree, and the disagreement is the signal
# --------------------------------------------------------------------------

def test_apex_flip_is_preferred_when_present():
    out = client().levels("SPY")
    assert out["flip"] == 745.9 and out["flip_source"] == "apex"
    assert out["flip_gex"] == 745.6, "the gex flip is still reported, not discarded"


def test_flip_split_only_when_the_two_put_price_on_opposite_sides():
    out = client().levels("SPY")           # spot 746.48, both flips below it
    assert out["flip_split"] is False

    straddling = client(apex={"gammaFlip": 745.9, "levels": APEX["levels"]},
                        gex=dict(GEX, gammaFlipLevel=747.9))
    assert straddling.levels("SPY")["flip_split"] is True


def test_above_flip_is_derived_from_the_preferred_flip():
    assert client().levels("SPY")["above_flip"] is True


# --------------------------------------------------------------------------
# Apex is premium; a gated call degrades rather than failing
# --------------------------------------------------------------------------

def test_gated_apex_degrades_to_the_gex_only_picture():
    out = client(apex_error="premium_required").levels("SPY")
    assert out["flip"] == 745.6 and out["flip_source"] == "gex"
    assert "apex" not in out
    assert "premium_required" in out["apex_note"]
    assert out["walls"], "the walls must still be there without apex"


def test_apex_levels_are_carried_when_present():
    apex = client().levels("SPY")["apex"]
    assert apex[0]["rank"] == 1 and apex[0]["score"] == 100.0


# --------------------------------------------------------------------------
# Failure modes never raise
# --------------------------------------------------------------------------

def test_no_api_key_reports_rather_than_raising():
    assert "TD_API_KEY" in T.TDPro(api_key="").levels("SPY")["error"]


@pytest.mark.parametrize("symbol", ["", "   ", None])
def test_empty_symbol_is_rejected(symbol):
    assert "error" in T.TDPro(api_key="x").levels(symbol)


def test_upstream_failure_is_reported_as_an_error_field():
    c = T.TDPro(api_key="x")

    def boom(tool, args=None, ttl=None):
        raise T.TDProError("rate limited")

    c.cached = boom
    assert c.levels("SPY")["error"] == "rate limited"


def test_unexpected_payload_shape_is_caught():
    c = T.TDPro(api_key="x")
    c.cached = lambda tool, args=None, ttl=None: "not a dict"
    assert "error" in c.levels("SPY")


def test_missing_spot_yields_no_walls():
    out = client(gex=dict(GEX, spotPrice=None)).levels("SPY")
    assert out["walls"] == []
