"""Alert crossing logic against a stub price/level source — no network, no account.

CLAUDE.md rule 4d names two behaviours that "were wrong on the first pass" and
must not be simplified away. This file is what makes that claim true:

  1. Never test `price <= level`. Alerts fire on a CROSSING, so one armed on the
     side price has already passed starts `pending` instead of firing instantly.
  2. A moving level must never fire an alert on its own. If the flip moves past
     a stationary price, price did not break anything.

Both are easy to "fix" into a plain comparison, and both would then misfire on
every alert armed near current price — which is most of them.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Point the store at a temp dir before importing: alerts.py resolves STORE_PATH
# at import time, and a test run must never touch the real alerts.json.
_TMP = tempfile.mkdtemp(prefix="sidecar-test-alerts-")
os.environ["SIDECAR_STATE_DIR"] = _TMP

import alerts as A  # noqa: E402


def check(label, fn):
    try:
        fn()
        print(f"  PASS  {label}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {label}: {e}")
        return False
    except Exception as e:
        print(f"  ERROR {label}: {type(e).__name__}: {e}")
        return False


ok = []


def feed(a, *prices, level=100.0):
    """Push a price series at one alert. Returns the fire records."""
    out = []
    for p in prices:
        lvl = level(p) if callable(level) else level
        rec = A.evaluate(a, p, lvl)
        if rec:
            out.append(rec)
    return out


print("\n== make_alert validation ==")


def t_static():
    a = A.make_alert("onds", 8.5, "below")
    assert a["symbol"] == "ONDS", a
    assert a["level_static"] == 8.5 and a["level_ref"] is None, a
    assert a["state"] == "pending", a
    assert len(a["id"]) == 12, a
ok.append(check("static level, symbol upper-cased, starts pending", t_static))


def t_dynamic():
    for ref in A.DYNAMIC_LEVELS:
        a = A.make_alert("SPY", ref, "below")
        assert a["level_ref"] == ref and a["level_static"] is None, a
ok.append(check("every dynamic level accepted", t_dynamic))


def t_bad_symbol():
    for bad in ("", "TOOLONGX", "SP Y", "123"):
        try:
            A.make_alert(bad, 1.0, "below")
            raise AssertionError(f"should reject symbol {bad!r}")
        except A.AlertError:
            pass
ok.append(check("bad symbols rejected", t_bad_symbol))


def t_bad_direction():
    try:
        A.make_alert("SPY", 1.0, "sideways")
        raise AssertionError("should reject direction")
    except A.AlertError as e:
        assert "direction" in str(e), e
ok.append(check("bad direction rejected", t_bad_direction))


def t_bad_level():
    for bad in ("gamma_flip", "abc", -5, 0):
        try:
            A.make_alert("SPY", bad, "below")
            raise AssertionError(f"should reject level {bad!r}")
        except A.AlertError:
            pass
ok.append(check("non-positive and unknown levels rejected", t_bad_level))


print("\n== invariant 1: a crossing, never `price <= level` ==")


def t_no_instant_fire():
    # Armed BELOW 100 while price is already at 95. A plain `price <= level`
    # comparison fires here; a crossing must not.
    a = A.make_alert("SPY", 100.0, "below")
    fired = feed(a, 95.0, 94.0, 93.0)
    assert not fired, f"fired without a crossing: {fired}"
    assert a["state"] == "pending", a["state"]
ok.append(check("armed on the wrong side does not fire instantly", t_no_instant_fire))


def t_arms_then_fires():
    a = A.make_alert("SPY", 100.0, "below")
    feed(a, 95.0)              # pending — wrong side
    feed(a, 105.0)             # arms: price now on the arming side
    assert a["state"] == "armed", a["state"]
    fired = feed(a, 99.0)      # genuine break below
    assert len(fired) == 1, fired
    assert fired[0]["price"] == 99.0 and fired[0]["prev_price"] == 105.0, fired
    assert a["state"] == "triggered", a["state"]
ok.append(check("returns to arming side, then fires on the break", t_arms_then_fires))


def t_above_direction():
    a = A.make_alert("SPY", 100.0, "above")
    feed(a, 105.0)             # pending — already above
    feed(a, 95.0)              # arms below
    fired = feed(a, 101.0)     # breaks up through
    assert len(fired) == 1, fired
ok.append(check("direction=above is the mirror image", t_above_direction))


def t_no_fire_without_prev():
    # Arming and crossing in the same evaluation has no previous price to cross
    # from, so it must not fire on that tick.
    a = A.make_alert("SPY", 100.0, "below")
    fired = feed(a, 105.0)
    assert not fired and a["state"] == "armed", (fired, a["state"])
ok.append(check("no fire on the tick that arms", t_no_fire_without_prev))


def t_one_shot():
    a = A.make_alert("SPY", 100.0, "below")
    feed(a, 105.0)
    assert len(feed(a, 99.0)) == 1
    # further movement must not re-fire a one-shot
    assert not feed(a, 105.0, 98.0, 105.0, 97.0), "one-shot re-fired"
    assert a["trigger_count"] == 1, a["trigger_count"]
ok.append(check("one-shot fires exactly once", t_one_shot))


def t_repeat_cooldown():
    a = A.make_alert("SPY", 100.0, "below", repeat=True, cooldown=9999.0)
    feed(a, 105.0)
    assert len(feed(a, 99.0)) == 1
    # inside the cooldown, no second fire
    assert not feed(a, 105.0, 99.0), "fired inside cooldown"
    # with the cooldown elapsed it re-arms and can fire again
    a["cooldown"] = 0.0
    feed(a, 105.0)
    assert len(feed(a, 99.0)) == 1, "did not re-fire after cooldown"
ok.append(check("repeat honours cooldown, then re-fires", t_repeat_cooldown))


print("\n== invariant 2: a moving level must not fire on its own ==")


def t_moving_level_does_not_fire():
    # Price is stationary at 100. The level (a dynamic flip) climbs from 95 to
    # 105, ending up above price. The alert is "below", so it looks like a
    # breakdown if you compare against the OLD level — but price never moved.
    a = A.make_alert("SPY", "flip", "below")
    A.evaluate(a, 100.0, 95.0)   # price above flip -> arms
    assert a["state"] == "armed", a["state"]
    fired = A.evaluate(a, 100.0, 105.0)  # flip jumps over a stationary price
    assert fired is None, f"a moving level fired an alert: {fired}"
    assert a["state"] == "pending", a["state"]
ok.append(check("level jumping across a stationary price does not fire", t_moving_level_does_not_fire))


def t_repends_then_fires_on_real_move():
    a = A.make_alert("SPY", "flip", "below")
    A.evaluate(a, 100.0, 95.0)            # armed
    A.evaluate(a, 100.0, 105.0)           # level moved over price -> pending
    A.evaluate(a, 110.0, 105.0)           # price genuinely back above -> armed
    assert a["state"] == "armed", a["state"]
    rec = A.evaluate(a, 104.0, 105.0)     # real break below
    assert rec is not None, "should fire on a genuine crossing after re-pending"
ok.append(check("re-pends, then fires on a genuine crossing", t_repends_then_fires_on_real_move))


def t_both_prices_against_current_level():
    # prev_price 96 was below the OLD level 95? No — it was above. If evaluate
    # compared prev against the old level and now against the new one, this
    # would read as a crossing. Judged against the CURRENT level, both are
    # below, so there is nothing to report.
    a = A.make_alert("SPY", "flip", "below")
    A.evaluate(a, 96.0, 95.0)    # above 95 -> armed
    rec = A.evaluate(a, 97.0, 100.0)  # level moved to 100; both 96 and 97 below it
    assert rec is None, f"manufactured a crossing from a level move: {rec}"
ok.append(check("both prices judged against the current level", t_both_prices_against_current_level))


print("\n== resolve_level: an outage silences, never substitutes ==")


def t_static_always_resolves():
    a = A.make_alert("SPY", 743.0, "below")
    assert A.resolve_level(a, None) == 743.0
ok.append(check("static level needs no structure", t_static_always_resolves))


def t_dynamic_none_when_unavailable():
    a = A.make_alert("SPY", "flip", "below")
    a["last_level"] = 743.0  # a remembered number that must NOT be reused
    assert A.resolve_level(a, None) is None, "fell back to a stale level"
    assert A.resolve_level(a, {"error": "rate limited"}) is None, "used an errored payload"
ok.append(check("dynamic level returns None on outage, not a stale number", t_dynamic_none_when_unavailable))


def t_walls():
    lv = {"spot": 100.0, "walls": [{"strike": 90.0}, {"strike": 95.0},
                                   {"strike": 105.0}, {"strike": 110.0}]}
    above = A.make_alert("SPY", "wall_above", "above")
    below = A.make_alert("SPY", "wall_below", "below")
    assert A.resolve_level(above, lv) == 105.0, "wall_above must be the NEAREST above spot"
    assert A.resolve_level(below, lv) == 95.0, "wall_below must be the NEAREST below spot"
ok.append(check("wall_above/below pick the nearest strike to spot", t_walls))


def t_no_walls():
    a = A.make_alert("SPY", "wall_above", "above")
    assert A.resolve_level(a, {"spot": 100.0, "walls": []}) is None
    assert A.resolve_level(a, {"spot": None, "walls": [{"strike": 1.0}]}) is None
ok.append(check("missing spot or walls resolves to None", t_no_walls))


def t_evaluate_needs_both_halves():
    a = A.make_alert("SPY", "flip", "below")
    assert A.evaluate(a, None, 100.0) is None
    assert A.evaluate(a, 100.0, None) is None
    assert A.evaluate(a, 0, 100.0) is None
ok.append(check("evaluate refuses to judge without price and level", t_evaluate_needs_both_halves))


print("\n== store ==")


def t_store_roundtrip():
    p = Path(_TMP) / "roundtrip.json"
    s = A.AlertStore(p)
    a = s.add(A.make_alert("SPY", 100.0, "below"))
    assert len(s.list()) == 1
    assert json.loads(p.read_text())[0]["id"] == a["id"]
    # a second store reading the same file sees it
    assert len(A.AlertStore(p).list()) == 1
    assert s.remove(a["id"]) is True
    assert s.remove(a["id"]) is False
    assert A.AlertStore(p).list() == []
ok.append(check("add/remove persists atomically", t_store_roundtrip))


def t_store_corrupt():
    p = Path(_TMP) / "corrupt.json"
    p.write_text("{not json")
    assert A.AlertStore(p).list() == [], "corrupt store should read as empty, not crash"
ok.append(check("corrupt store file degrades to empty", t_store_corrupt))


def t_sweep_isolates_failures():
    p = Path(_TMP) / "sweep.json"
    s = A.AlertStore(p)
    s.add(A.make_alert("BAD", 100.0, "below"))
    s.add(A.make_alert("GOOD", 100.0, "below"))

    def price_of(sym):
        if sym == "BAD":
            raise RuntimeError("quote source exploded")
        return 105.0

    s.sweep(price_of, lambda sym: None)          # arms GOOD, skips BAD
    fired = s.sweep(lambda sym: 95.0 if sym == "GOOD" else 1 / 0, lambda sym: None)
    assert len(fired) == 1 and fired[0]["symbol"] == "GOOD", fired
ok.append(check("one bad symbol does not stop the sweep", t_sweep_isolates_failures))


def t_sweep_skips_spent_alerts():
    p = Path(_TMP) / "spent.json"
    s = A.AlertStore(p)
    s.add(A.make_alert("SPY", 100.0, "below"))
    s.sweep(lambda _: 105.0, lambda _: None)
    assert len(s.sweep(lambda _: 95.0, lambda _: None)) == 1
    # already triggered and not repeating: never evaluated again
    assert not s.sweep(lambda _: 105.0, lambda _: None)
    assert not s.sweep(lambda _: 95.0, lambda _: None)
ok.append(check("triggered one-shots are skipped by later sweeps", t_sweep_skips_spent_alerts))


print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
