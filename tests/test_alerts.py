"""Alert crossing logic.

This is the most bug-prone code in the repo and the two properties below were
both wrong on the first implementation, so they are pinned here rather than
left to a comment. See CLAUDE.md rule 4d.
"""

from __future__ import annotations

import pytest

import alerts as A


def feed(alert, steps):
    """Push (price, level) pairs through evaluate(); return prices that fired."""
    out = []
    for price, level in steps:
        rec = A.evaluate(alert, price, level)
        if rec:
            out.append(round(rec["price"], 2))
    return out


# --------------------------------------------------------------------------
# A break is a TRANSITION, not a comparison
# --------------------------------------------------------------------------

def test_break_below_fires_on_the_crossing_bar():
    a = A.make_alert("SPY", 743, "below")
    assert feed(a, [(746, 743), (744, 743), (742, 743), (740, 743)]) == [742]
    assert a["state"] == "triggered"


def test_break_above_is_the_mirror():
    a = A.make_alert("QQQ", 600, "above")
    assert feed(a, [(598, 600), (599, 600), (601, 600)]) == [601]


def test_armed_on_the_wrong_side_does_not_fire_instantly():
    """`price <= level` would fire here. That is the bug this prevents."""
    a = A.make_alert("SPY", 743, "below")
    assert feed(a, [(741, 743), (740, 743), (739, 743)]) == []
    assert a["state"] == "pending", "must wait to arm, not fire and not silently arm"


def test_pending_alert_arms_then_fires_on_a_real_break():
    a = A.make_alert("SPY", 743, "below")
    feed(a, [(741, 743)])                      # starts pending
    assert feed(a, [(745, 743), (742, 743)]) == [742]


def test_oscillation_on_the_level_fires_once():
    a = A.make_alert("SPY", 743, "below")
    steps = [(744, 743), (742, 743)] * 3
    assert feed(a, steps) == [742], "one-shot must not re-fire while price chops"


# --------------------------------------------------------------------------
# A MOVING level must not fire the alert by itself.
# Unique to gamma-aware alerts; no broker implementation has this problem.
# --------------------------------------------------------------------------

def test_level_moving_past_a_stationary_price_does_not_fire():
    a = A.make_alert("SPY", "flip", "below")
    # Price pinned at 746.50 throughout; the flip moves up over it.
    assert feed(a, [(746.5, 745.0), (746.5, 748.0), (746.5, 748.0)]) == []


def test_level_moving_past_price_re_pends_rather_than_lying_about_being_armed():
    a = A.make_alert("SPY", "flip", "below")
    feed(a, [(746.5, 745.0), (746.5, 748.0)])
    assert a["state"] == "pending", "an alert on the wrong side of its level is not armed"


def test_a_genuine_break_of_the_moved_level_still_fires():
    a = A.make_alert("SPY", "flip", "below")
    feed(a, [(746.5, 745.0), (746.5, 748.0)])   # level jumped over price
    assert feed(a, [(752.0, 748.0), (746.0, 748.0)]) == [746.0]


# --------------------------------------------------------------------------
# Repeat / cooldown
# --------------------------------------------------------------------------

def test_one_shot_stays_triggered():
    a = A.make_alert("SPY", 743, "below")
    feed(a, [(746, 743), (742, 743)])
    assert feed(a, [(746, 743), (742, 743)]) == []


def test_repeat_refires_after_cooldown():
    a = A.make_alert("SPY", 743, "below", repeat=True, cooldown=0.0)
    feed(a, [(746, 743), (742, 743)])
    assert feed(a, [(746, 743), (742, 743)]) == [742]


def test_repeat_respects_cooldown():
    a = A.make_alert("SPY", 743, "below", repeat=True, cooldown=3600.0)
    feed(a, [(746, 743), (742, 743)])
    assert feed(a, [(746, 743), (742, 743)]) == [], "cooldown must suppress the second fire"


# --------------------------------------------------------------------------
# Missing data is never a fire
# --------------------------------------------------------------------------

@pytest.mark.parametrize("price,level", [(None, 745), (744, None), (None, None), (0, 745), (744, 0)])
def test_missing_or_zero_inputs_never_fire(price, level):
    a = A.make_alert("SPY", "flip", "below")
    feed(a, [(746, 745)])
    assert feed(a, [(price, level)]) == []


# --------------------------------------------------------------------------
# Dynamic level resolution
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("flip", 745.6), ("pin", 743.0), ("wall_above", 750.0), ("wall_below", 743.0),
])
def test_dynamic_levels_resolve(levels, ref, expected):
    assert A.resolve_level(A.make_alert("SPY", ref, "below"), levels) == expected


def test_wall_refs_pick_the_NEAREST_wall_each_side(levels):
    """Not the biggest, and not the furthest — the one price meets first."""
    assert A.resolve_level(A.make_alert("SPY", "wall_above", "above"), levels) == 750.0
    assert A.resolve_level(A.make_alert("SPY", "wall_below", "below"), levels) == 743.0


def test_unavailable_structure_resolves_to_None_not_a_stale_number():
    """A remembered flip is the exact failure this module exists to prevent."""
    a = A.make_alert("SPY", "flip", "below")
    assert A.resolve_level(a, {"error": "rate limited"}) is None
    assert A.resolve_level(a, None) is None


def test_static_level_survives_a_dead_tdpro(levels):
    a = A.make_alert("SPY", 743, "below")
    assert A.resolve_level(a, {"error": "down"}) == 743.0


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ("", 743, "below"), ("TOOLONGX", 743, "below"), ("SP Y", 743, "below"),
    ("SPY", 743, "sideways"), ("SPY", "banana", "below"),
    ("SPY", -5, "below"), ("SPY", 0, "below"),
])
def test_bad_input_is_rejected(args):
    with pytest.raises(A.AlertError):
        A.make_alert(*args)


def test_symbol_is_normalised():
    assert A.make_alert("spy", 743, "below")["symbol"] == "SPY"


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def test_store_round_trips(tmp_path):
    path = tmp_path / "alerts.json"
    s = A.AlertStore(path)
    s.add(A.make_alert("SPY", "flip", "below"))
    s.add(A.make_alert("GLD", 62.5, "above"))
    assert len(A.AlertStore(path).list()) == 2


def test_store_symbols_excludes_spent_one_shots(tmp_path):
    s = A.AlertStore(tmp_path / "a.json")
    done = A.make_alert("SPY", 743, "below")
    done["state"] = "triggered"
    s.add(done)
    s.add(A.make_alert("GLD", 62.5, "above"))
    assert s.symbols() == ["GLD"], "a spent alert must not keep its symbol on the poll list"


def test_corrupt_store_boots_empty_rather_than_crashing(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{not json")
    assert A.AlertStore(path).list() == []


def test_remove_reports_whether_it_removed(tmp_path):
    s = A.AlertStore(tmp_path / "a.json")
    a = s.add(A.make_alert("SPY", 743, "below"))
    assert s.remove(a["id"]) is True
    assert s.remove(a["id"]) is False


def test_sweep_survives_one_bad_symbol(tmp_path, levels):
    """One broken lookup must not stop the other alerts being evaluated."""
    s = A.AlertStore(tmp_path / "a.json")
    s.add(A.make_alert("BAD", 100, "below"))
    s.add(A.make_alert("SPY", 743, "below"))

    def price_of(sym):
        if sym == "BAD":
            raise RuntimeError("quote source exploded")
        return 746.0

    s.sweep(price_of, lambda sym: levels)          # arms SPY
    fired = s.sweep(lambda sym: 742.0 if sym == "SPY" else (_ for _ in ()).throw(RuntimeError()),
                    lambda sym: levels)
    assert [f["symbol"] for f in fired] == ["SPY"]
