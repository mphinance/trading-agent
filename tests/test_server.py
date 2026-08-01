"""HTTP routes and the end-to-end watcher lifecycle.

Exercises the real server module against stubbed data sources, so the routes,
the store, the watcher and the notifier are all the production objects.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import alerts as A
import notify


@pytest.fixture
def app(levels, tmp_path):
    """The real server, wired to controllable price and structure.

    The store is rebuilt per test against tmp_path. `alerts.STATE_DIR` is
    resolved at import time, so setting SIDECAR_STATE_DIR in a fixture cannot
    move it — without an explicit path every test would share one alerts.json
    and inherit the previous test's armed alerts.
    """
    import server

    state = {"price": {"SPY": 746.5}, "flip": 745.0, "sent": []}

    def levels_of(symbol):
        return dict(levels, symbol=symbol.upper(),
                    spot=state["price"].get(symbol.upper(), 100.0),
                    flip=state["flip"])

    class Capture(notify._Channel):
        name = "capture"
        configured = True

        def status(self):
            return {"name": self.name, "configured": True}

        def send(self, text, title=""):
            state["sent"].append((title, text))
            return True

    server._store = A.AlertStore(tmp_path / "alerts.json")
    server._watcher.store = server._store
    server._td.levels = levels_of
    server._watcher.levels_of = levels_of
    server._quotes.get = lambda sym, max_age=0: state["price"].get(sym.upper())
    server._quotes.refresh = lambda syms: None
    server._quotes.source_of = lambda sym: "webull"
    server._quotes.age_of = lambda sym: 1.0
    server._quotes.status = lambda: {"snapshot": "ok", "cached": 0}
    server._watcher.notifier = notify.Notifier(channels=[Capture()])
    server._watcher.notifier._last_send = -999

    with TestClient(server.app) as client:
        yield client, server, state


# --------------------------------------------------------------------------
# /api/gex
# --------------------------------------------------------------------------

def test_gex_returns_the_compacted_structure(app):
    client, _, _ = app
    r = client.get("/api/gex/spy")
    assert r.status_code == 200
    assert r.json()["symbol"] == "SPY", "symbol must be normalised"


@pytest.mark.parametrize("bad", ["SP500X", "SP Y", "1234", "SPY!"])
def test_gex_rejects_malformed_symbols(app, bad):
    client, _, _ = app
    assert client.get(f"/api/gex/{bad}").status_code == 400


@pytest.mark.parametrize("bad", ["../etc/passwd", ""])
def test_gex_does_not_route_path_traversal(app, bad):
    client, _, _ = app
    assert client.get(f"/api/gex/{bad}").status_code in (400, 404)


# --------------------------------------------------------------------------
# Alert routes
# --------------------------------------------------------------------------

def test_creating_an_alert_seeds_its_state_immediately(app):
    client, _, _ = app
    r = client.post("/api/alerts", json={"symbol": "SPY", "level": "flip", "direction": "below"})
    body = r.json()
    assert r.status_code == 200
    assert body["state"] == "armed", "price is above the flip, so it arms at once"
    assert body["level_now"] == 745.0


def test_an_alert_on_the_wrong_side_is_created_pending_not_triggered(app):
    client, _, _ = app
    body = client.post("/api/alerts",
                       json={"symbol": "SPY", "level": 900, "direction": "below"}).json()
    assert body["state"] == "pending"


def test_numeric_and_named_levels_are_both_accepted(app):
    client, _, _ = app
    assert client.post("/api/alerts", json={"symbol": "SPY", "level": 743.5,
                                            "direction": "below"}).status_code == 200
    assert client.post("/api/alerts", json={"symbol": "SPY", "level": "pin",
                                            "direction": "above"}).status_code == 200


@pytest.mark.parametrize("payload", [
    {"symbol": "SPY", "level": "banana", "direction": "below"},
    {"symbol": "SPY", "level": 700, "direction": "sideways"},
    {"symbol": "", "level": 700, "direction": "below"},
])
def test_bad_alert_input_is_a_400_with_a_reason(app, payload):
    client, _, _ = app
    r = client.post("/api/alerts", json=payload)
    assert r.status_code == 400 and r.json()["detail"]


def test_delete_removes_and_404s_the_second_time(app):
    client, _, _ = app
    aid = client.post("/api/alerts",
                      json={"symbol": "SPY", "level": 743, "direction": "below"}).json()["id"]
    assert client.delete(f"/api/alerts/{aid}").status_code == 200
    assert client.delete(f"/api/alerts/{aid}").status_code == 404


def test_listing_includes_the_live_resolved_level(app):
    client, _, state = app
    client.post("/api/alerts", json={"symbol": "SPY", "level": "flip", "direction": "below"})
    state["flip"] = 700.0
    row = client.get("/api/alerts").json()["alerts"][0]
    assert row["level_now"] == 700.0, "the level must be re-resolved, not remembered"


def test_the_topic_never_reaches_the_browser(app):
    """Rule 5: this panel is streamed."""
    client, _, _ = app
    blob = str(client.get("/api/alerts").json())
    assert "NTFY_TOPIC" not in blob and "sidecar-" not in blob


# --------------------------------------------------------------------------
# Watcher lifecycle, end to end
# --------------------------------------------------------------------------

def test_full_lifecycle_arm_repend_rearm_fire(app):
    client, server, state = app
    client.post("/api/alerts", json={"symbol": "SPY", "level": "flip",
                                     "direction": "below", "note": "trending down"})

    def state_now():
        return client.get("/api/alerts").json()["alerts"][0]["state"]

    server._watcher._tick()
    assert state_now() == "armed" and not state["sent"]

    # The flip moves up over a stationary price: not a break.
    state["flip"] = 748.0
    server._watcher._tick()
    assert not state["sent"], "a moving level must not fire the alert"
    assert state_now() == "pending"

    # Price returns above the moved flip, then genuinely breaks it.
    state["price"]["SPY"] = 752.0
    server._watcher._tick()
    assert state_now() == "armed"

    state["price"]["SPY"] = 746.0
    server._watcher._tick()
    assert len(state["sent"]) == 1
    title, body = state["sent"][0]
    assert "SPY" in title and "broke below flip" in body and "trending down" in body
    assert state_now() == "triggered"

    # One-shot must not re-fire.
    state["price"]["SPY"] = 760.0
    server._watcher._tick()
    state["price"]["SPY"] = 700.0
    server._watcher._tick()
    assert len(state["sent"]) == 1


def test_a_tdpro_outage_silences_dynamic_alerts_rather_than_misfiring(app):
    client, server, state = app
    client.post("/api/alerts", json={"symbol": "SPY", "level": "flip", "direction": "below"})
    server._watcher._tick()
    server._watcher.levels_of = lambda sym: {"error": "rate limited"}
    state["price"]["SPY"] = 1.0          # would cross any real level
    for _ in range(3):
        server._watcher._tick()
    assert not state["sent"], "a stale level is the failure this design prevents"


def test_watcher_status_reports_what_it_is_watching(app):
    client, server, _ = app
    client.post("/api/alerts", json={"symbol": "SPY", "level": 743, "direction": "below"})
    w = client.get("/api/alerts").json()["watcher"]
    assert "SPY" in w["watching"]
    assert w["notify"]["configured"] is True


def test_test_route_exercises_the_delivery_path(app):
    client, _, state = app
    r = client.post("/api/alerts/test")
    assert r.json()["sent"] is True and state["sent"]
