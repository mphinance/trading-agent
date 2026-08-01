"""The order path — the only thing here that can move money.

Rule 3 used to read "there is no order path, keep it that way." It was reversed
deliberately, so these tests are the other half of that change: they pin the
three properties the new rule leans on, and they fail loudly if any of them is
quietly simplified away.

  1. Preview stages a ticket; place takes a ticket_id, never an order. So no
     single call can both construct and fire, and a ticket is single-use.
  2. The caps run server-side on every path — including replace, because
     amending a working order can raise exposure. Cancel is never capped.
  3. A market order is priced from the live quote before the cap is applied, so
     omitting a limit price cannot dodge it.

No network, no broker, no credentials: the SDK client is a stub that records
what it was handed. What it records is also the payload assertion — Webull's
field shapes are the thing most likely to be wrong on the first live order.
"""

from __future__ import annotations

import pytest

import orders as O


class Res:
    """Stand-in for the SDK's requests.Response."""

    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b


class StubOps:
    """Records every call so a test can assert on the exact payload sent."""

    def __init__(self):
        self.calls = []

    def preview_order(self, aid, payload):
        self.calls.append(("preview", aid, payload))
        return Res({"estimated_cost": "188.00"})

    def place_order(self, aid, payload):
        self.calls.append(("place", aid, payload))
        return Res({"order_id": "X1"})

    def batch_place_order(self, aid, payload):
        self.calls.append(("batch", aid, payload))
        return Res({"ok": True})

    def replace_order(self, aid, payload):
        self.calls.append(("replace", aid, payload))
        return Res({"ok": True})

    def cancel_order(self, aid, coid):
        self.calls.append(("cancel", aid, coid))
        return Res({"ok": True})

    preview_option = preview_order
    place_option = place_order
    replace_option = replace_order
    cancel_option = cancel_order


class StubWebull:
    def __init__(self):
        self.v3, self.v2 = StubOps(), StubOps()
        self.trade = type("T", (), {"order_v3": self.v3, "order_v2": self.v2})()
        self.invalidated = []

    def account_ids(self):
        return ["ACC1"]

    def invalidate(self, *keys):
        self.invalidated.extend(keys)

    def portfolio(self):
        return {"totals": {"buying_power": 400.0},
                "positions": [{"symbol": "AAPL", "last_price": 188.0}]}


@pytest.fixture
def wb():
    return StubWebull()


@pytest.fixture
def orders(wb):
    return O.Orders(wb)


LIMIT_BUY = {"symbol": "AAPL", "side": "BUY", "quantity": 1,
             "order_type": "LIMIT", "limit_price": 188}


# --- payload shape ---------------------------------------------------------
# These are what Webull actually receives. Getting a field name or a type wrong
# here is the most likely way the first live order gets rejected.


def test_a_simple_limit_order_has_the_fields_webull_expects(orders):
    payload = orders.build_equity(dict(LIMIT_BUY, symbol="aapl", side="buy"))
    assert len(payload) == 1
    o = payload[0]
    assert o["symbol"] == "AAPL" and o["side"] == "BUY"
    assert o["combo_type"] == "NORMAL" and o["instrument_type"] == "EQUITY"
    assert o["entrust_type"] == "QTY" and o["support_trading_session"] == "CORE"
    assert o["quantity"] == "1" and o["limit_price"] == "188"
    assert len(o["client_order_id"]) == 32


def test_no_field_is_ever_none(orders):
    """Webull rejects nulls, so an unset field must be omitted, not sent empty."""
    payload = orders.build_equity({"symbol": "AAPL", "side": "BUY", "quantity": 1,
                                   "order_type": "MARKET"})
    assert all(v is not None for v in payload[0].values())
    assert "limit_price" not in payload[0]


def test_quantities_are_not_rendered_as_floats(orders):
    """MCP hands every number over as a float.

    Left alone that puts "2.0" in the payload and, worse, in the summary Claude
    reads back out loud before you confirm.
    """
    payload = orders.build_equity(dict(LIMIT_BUY, quantity=2.0, limit_price=8.40))
    assert payload[0]["quantity"] == "2"
    payload = orders.build_equity(dict(LIMIT_BUY, quantity=1.5))
    assert payload[0]["quantity"] == "1.5"


def test_a_bracket_becomes_a_webull_combo(orders):
    payload = orders.build_equity(dict(LIMIT_BUY, symbol="F", limit_price=10.5,
                                       bracket={"take_profit": 11.5, "stop_loss": 10}))
    assert [o["combo_type"] for o in payload] == ["MASTER", "STOP_PROFIT", "STOP_LOSS"]
    assert payload[1]["limit_price"] == "11.5"
    assert payload[2]["order_type"] == "STOP_LOSS" and payload[2]["stop_price"] == "10"
    # children close the position, so they take the opposite side and same size
    assert payload[1]["side"] == payload[2]["side"] == "SELL"
    assert {o["quantity"] for o in payload} == {"1"}
    assert len({o["client_order_id"] for o in payload}) == 3


def test_a_bracket_on_a_sell_closes_with_a_buy(orders):
    payload = orders.build_equity({"symbol": "F", "side": "SELL", "quantity": 2,
                                   "order_type": "LIMIT", "limit_price": 10.5,
                                   "bracket": {"stop_loss": 11}})
    assert payload[1]["side"] == "BUY"


def test_algo_orders_carry_their_participation_parameter(orders):
    payload = orders.build_equity(dict(LIMIT_BUY, quantity=10, limit_price=100,
                                       algo_type="TWAP", max_target_percent=20,
                                       algo_start_time="09:30:00"))
    assert payload[0]["algo_type"] == "TWAP"
    assert payload[0]["max_target_percent"] == "20"
    assert payload[0]["algo_start_time"] == "09:30:00"


def test_a_single_leg_option_order(orders):
    payload = orders.build_option({
        "quantity": 1, "limit_price": 21.25, "side": "BUY", "time_in_force": "GTC",
        "legs": [{"symbol": "TSLA", "strike_price": 400, "option_expire_date": "2026-12-26",
                  "option_type": "CALL", "side": "BUY", "quantity": 1}]})
    o = payload[0]
    assert o["option_strategy"] == "SINGLE" and o["limit_price"] == "21.25"
    leg = o["legs"][0]
    assert leg["instrument_type"] == "OPTION" and leg["market"] == "US"
    assert leg["strike_price"] == "400"
    assert leg["option_expire_date"] == "2026-12-26"


def test_a_multi_leg_option_order_keeps_leg_order_and_sides(orders):
    payload = orders.build_option({
        "quantity": 1, "limit_price": 1.10, "side": "BUY", "option_strategy": "VERTICAL",
        "legs": [
            {"symbol": "SPY", "strike_price": 500, "option_expire_date": "2026-09-18",
             "option_type": "CALL", "side": "BUY", "quantity": 1},
            {"symbol": "SPY", "strike_price": 505, "option_expire_date": "2026-09-18",
             "option_type": "CALL", "side": "SELL", "quantity": 1}]})
    legs = payload[0]["legs"]
    assert [l["side"] for l in legs] == ["BUY", "SELL"]
    assert [l["strike_price"] for l in legs] == ["500", "505"]


def test_an_option_order_needs_at_least_one_leg(orders):
    with pytest.raises(O.OrderError):
        orders.build_option({"quantity": 1, "legs": []})


# --- the guards ------------------------------------------------------------


def test_the_notional_cap_rejects_before_webull_sees_it(orders, wb):
    with pytest.raises(O.OrderError, match="MAX_NOTIONAL"):
        orders.preview(dict(LIMIT_BUY, quantity=100))
    assert wb.v3.calls == [], "a capped order still reached the broker"


def test_a_market_order_is_priced_before_the_cap_is_applied(orders):
    """No limit price must not mean no cap.

    The stub portfolio prices AAPL at 188, so 100 shares is well over the
    default $2500 — a cap that only looked at limit_price would wave this
    through.
    """
    with pytest.raises(O.OrderError, match="MAX_NOTIONAL"):
        orders.preview({"symbol": "AAPL", "side": "BUY", "quantity": 100,
                        "order_type": "MARKET"})


@pytest.mark.parametrize("spec,expected", [
    ({"symbol": "AAPL", "side": "BUY", "quantity": 0, "order_type": "MARKET"}, "quantity"),
    ({"symbol": "AAPL", "side": "YOLO", "quantity": 1, "order_type": "MARKET"}, "side"),
    ({"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT"}, "limit_price"),
    ({"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "STOP_LOSS"}, "stop_price"),
    ({"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "SHOUT"}, "order_type"),
    ({"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "MARKET",
      "time_in_force": "FOREVER"}, "time_in_force"),
])
def test_malformed_orders_are_rejected_with_the_offending_field(orders, spec, expected):
    with pytest.raises(O.OrderError, match=expected):
        orders.preview(spec)


def test_an_allowlist_blocks_everything_else(orders, monkeypatch):
    monkeypatch.setattr(O, "SYMBOL_ALLOWLIST", {"AAPL"})
    orders.preview(LIMIT_BUY)  # allowed
    with pytest.raises(O.OrderError, match="ALLOWLIST"):
        orders.preview(dict(LIMIT_BUY, symbol="TSLA"))


def test_the_kill_switch_stops_every_write(orders, monkeypatch):
    monkeypatch.setattr(O, "TRADING_ENABLED", False)
    for call in (lambda: orders.preview(LIMIT_BUY),
                 lambda: orders.place_ticket("whatever"),
                 lambda: orders.cancel("ACC1", "coid")):
        with pytest.raises(O.TradingDisabled):
            call()


# --- the ticket handshake --------------------------------------------------


def test_preview_stages_a_ticket_and_does_not_send(orders, wb):
    t = orders.preview(LIMIT_BUY)
    assert t["ticket_id"] and t["used"] is False
    assert t["summary"].startswith("BUY 1 AAPL @ 188")
    assert [c[0] for c in wb.v3.calls] == ["preview"], "preview placed an order"


def test_place_sends_exactly_what_preview_staged(orders, wb):
    t = orders.preview(LIMIT_BUY)
    orders.place_ticket(t["ticket_id"])
    kind, _, sent = wb.v3.calls[-1]
    assert kind == "place"
    assert sent == t["orders"], "what was confirmed is not what was sent"


def test_a_ticket_cannot_be_replayed(orders):
    t = orders.preview(LIMIT_BUY)
    orders.place_ticket(t["ticket_id"])
    with pytest.raises(O.OrderError, match="already placed"):
        orders.place_ticket(t["ticket_id"])


def test_an_unknown_ticket_is_refused(orders):
    with pytest.raises(O.OrderError, match="unknown"):
        orders.place_ticket("deadbeef")


def test_an_expired_ticket_is_refused(orders, monkeypatch):
    t = orders.preview(LIMIT_BUY)
    monkeypatch.setattr(O, "TICKET_TTL_SEC", -1)
    with pytest.raises(O.OrderError, match="expired"):
        orders.place_ticket(t["ticket_id"])


def test_a_tampered_ticket_is_refused(orders):
    """The digest is the point: the payload cannot change after you confirmed it."""
    t = orders.preview(LIMIT_BUY)
    ticket = orders._tickets[t["ticket_id"]]
    ticket.payload[0]["quantity"] = "9999"
    with pytest.raises(O.OrderError, match="changed since preview"):
        orders.place_ticket(t["ticket_id"])


def test_single_shot_placement_is_blocked_while_confirmation_is_on(orders):
    with pytest.raises(O.OrderError, match="confirmation required"):
        orders.place_direct(LIMIT_BUY)


def test_single_shot_placement_works_once_confirmation_is_off(orders, wb, monkeypatch):
    monkeypatch.setattr(O, "REQUIRE_CONFIRM", False)
    orders.place_direct(LIMIT_BUY)
    assert [c[0] for c in wb.v3.calls] == ["place"]


def test_a_broker_rejection_is_surfaced_verbatim_and_frees_the_ticket(wb):
    """The reason has to survive: it is what gets read back to the user."""
    orders = O.Orders(wb)
    t = orders.preview(LIMIT_BUY)

    def rejecting(aid, payload):
        return Res({"msg": "insufficient buying power"}, status=400)

    wb.v3.place_order = rejecting
    with pytest.raises(O.OrderError, match="insufficient buying power"):
        orders.place_ticket(t["ticket_id"])
    assert orders._tickets[t["ticket_id"]].used is False, \
        "a broker-side failure should not burn the confirmed ticket"


def test_a_fill_invalidates_the_cached_portfolio(orders, wb):
    t = orders.preview(LIMIT_BUY)
    orders.place_ticket(t["ticket_id"])
    assert "portfolio" in wb.invalidated and "open_orders" in wb.invalidated


def test_the_summary_names_the_bracket_legs(orders):
    """This line is what gets spoken before you say yes, so it must be complete."""
    t = orders.preview(dict(LIMIT_BUY, symbol="F", limit_price=10.5,
                            bracket={"take_profit": 11.5, "stop_loss": 10}))
    assert "TP 11.5" in t["summary"] and "SL 10" in t["summary"]


def test_a_discarded_ticket_cannot_be_placed(orders):
    t = orders.preview(LIMIT_BUY)
    assert orders.discard(t["ticket_id"]) is True
    with pytest.raises(O.OrderError):
        orders.place_ticket(t["ticket_id"])


# --- replace and cancel ----------------------------------------------------


def test_replace_re_runs_the_caps(orders):
    """Amending a working order can raise exposure, so it is not a free pass."""
    with pytest.raises(O.OrderError, match="MAX_NOTIONAL"):
        orders.replace("ACC1", "coid", {"symbol": "AAPL", "quantity": 500,
                                        "limit_price": 188})


def test_replace_with_nothing_to_change_is_rejected(orders):
    with pytest.raises(O.OrderError, match="nothing to change"):
        orders.replace("ACC1", "coid", {})


def test_cancel_is_never_capped(orders, wb):
    """Reducing risk is always allowed, whatever the caps say."""
    assert orders.cancel("ACC1", "coid")["ok"] is True
    assert ("cancel", "ACC1", "coid") in wb.v3.calls


def test_cancel_routes_options_to_the_v2_endpoint(orders, wb):
    orders.cancel("ACC1", "coid", kind="option")
    assert ("cancel", "ACC1", "coid") in wb.v2.calls
    assert ("cancel", "ACC1", "coid") not in wb.v3.calls


def test_config_reports_the_caps_actually_in_force(orders):
    cfg = O.Orders.config()
    assert cfg["trading_enabled"] is True
    assert cfg["require_confirm"] is True
    assert cfg["max_notional"] > 0
    # chat must never gain the order path by default — see rule 3
    assert cfg["chat_autotrade"] is False
