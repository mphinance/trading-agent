"""Exercise orders.py against a stub broker: payload shapes, guards, ticket flow."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orders as O


class Res:
    status_code = 200
    def __init__(self, body): self._b = body
    def json(self): return self._b


class StubOrderOps:
    def __init__(self): self.calls = []
    def preview_order(self, aid, payload): self.calls.append(("preview", aid, payload)); return Res({"est_cost": "188.00"})
    def place_order(self, aid, payload): self.calls.append(("place", aid, payload)); return Res({"order_id": "X1"})
    def batch_place_order(self, aid, payload): self.calls.append(("batch", aid, payload)); return Res({"ok": 1})
    def replace_order(self, aid, payload): self.calls.append(("replace", aid, payload)); return Res({"ok": 1})
    def cancel_order(self, aid, coid): self.calls.append(("cancel", aid, coid)); return Res({"ok": 1})
    preview_option = preview_order
    place_option = place_order
    replace_option = replace_order
    cancel_option = cancel_order


class StubTrade:
    def __init__(self): self.order_v3 = StubOrderOps(); self.order_v2 = StubOrderOps()


class StubWebull:
    def __init__(self):
        self.trade = StubTrade()
        self.invalidated = []
    def account_ids(self): return ["ACC1"]
    def invalidate(self, *k): self.invalidated.extend(k)
    def portfolio(self): return {"totals": {"buying_power": 400.0}, "positions": [
        {"symbol": "AAPL", "last_price": 188.0}]}


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
wbs = StubWebull()
o = O.Orders(wbs)

print("\n== payload shape ==")


def t_simple():
    p = o.build_equity({"symbol": "aapl", "side": "buy", "quantity": 1,
                        "order_type": "LIMIT", "limit_price": 188})
    assert len(p) == 1, p
    r = p[0]
    assert r["symbol"] == "AAPL", r
    assert r["combo_type"] == "NORMAL", r
    assert r["quantity"] == "1" and r["limit_price"] == "188", r
    assert r["entrust_type"] == "QTY" and r["support_trading_session"] == "CORE", r
    assert r["instrument_type"] == "EQUITY" and r["market"] == "US", r
    assert len(r["client_order_id"]) == 32, r
    # no stray None fields — Webull rejects nulls
    assert all(v is not None for v in r.values()), r
ok.append(check("simple limit buy", t_simple))


def t_bracket():
    p = o.build_equity({"symbol": "F", "side": "BUY", "quantity": 1, "order_type": "LIMIT",
                        "limit_price": 10.5, "bracket": {"take_profit": 11.5, "stop_loss": 10}})
    assert len(p) == 3, p
    assert p[0]["combo_type"] == "MASTER", p[0]
    assert p[1]["combo_type"] == "STOP_PROFIT" and p[1]["side"] == "SELL", p[1]
    assert p[1]["limit_price"] == "11.5", p[1]
    assert p[2]["combo_type"] == "STOP_LOSS" and p[2]["order_type"] == "STOP_LOSS", p[2]
    assert p[2]["stop_price"] == "10", p[2]
    assert p[1]["quantity"] == p[0]["quantity"] == "1", p
    # children need distinct client_order_ids
    ids = {x["client_order_id"] for x in p}
    assert len(ids) == 3, ids
ok.append(check("bracket -> MASTER/STOP_PROFIT/STOP_LOSS", t_bracket))


def t_bracket_sell():
    p = o.build_equity({"symbol": "F", "side": "SELL", "quantity": 2, "order_type": "LIMIT",
                        "limit_price": 10.5, "bracket": {"stop_loss": 11}})
    assert p[1]["side"] == "BUY", p[1]  # children close the opposite way
ok.append(check("bracket on a SELL closes with BUY", t_bracket_sell))


def t_algo():
    p = o.build_equity({"symbol": "AAPL", "side": "BUY", "quantity": 10, "order_type": "LIMIT",
                        "limit_price": 100, "algo_type": "TWAP", "max_target_percent": 20,
                        "algo_start_time": "09:30:00", "algo_end_time": "16:00:00"})
    r = p[0]
    assert r["algo_type"] == "TWAP" and r["max_target_percent"] == "20", r
    assert r["algo_start_time"] == "09:30:00", r
ok.append(check("TWAP algo fields", t_algo))


def t_option():
    p = o.build_option({"quantity": 1, "limit_price": 21.25, "side": "BUY", "time_in_force": "GTC",
                        "option_strategy": "SINGLE",
                        "legs": [{"symbol": "TSLA", "strike_price": 400, "option_expire_date": "2026-12-26",
                                  "option_type": "CALL", "side": "BUY", "quantity": 1}]})
    r = p[0]
    assert r["option_strategy"] == "SINGLE" and r["limit_price"] == "21.25", r
    leg = r["legs"][0]
    assert leg["instrument_type"] == "OPTION" and leg["market"] == "US", leg
    assert leg["strike_price"] == "400" and leg["option_expire_date"] == "2026-12-26", leg
ok.append(check("single-leg option", t_option))


def t_vertical():
    p = o.build_option({"quantity": 1, "limit_price": 1.10, "side": "BUY", "option_strategy": "VERTICAL",
                        "legs": [
                            {"symbol": "SPY", "strike_price": 500, "option_expire_date": "2026-09-18",
                             "option_type": "CALL", "side": "BUY", "quantity": 1},
                            {"symbol": "SPY", "strike_price": 505, "option_expire_date": "2026-09-18",
                             "option_type": "CALL", "side": "SELL", "quantity": 1}]})
    assert len(p[0]["legs"]) == 2, p
    assert p[0]["legs"][1]["side"] == "SELL", p
ok.append(check("two-leg vertical", t_vertical))

print("\n== guards ==")


def t_notional_cap():
    try:
        o.preview({"symbol": "AAPL", "side": "BUY", "quantity": 100, "order_type": "LIMIT",
                   "limit_price": 188})
        raise AssertionError("should have been capped")
    except O.OrderError as e:
        assert "MAX_NOTIONAL" in str(e), e
ok.append(check("notional cap trips at 100x188", t_notional_cap))


def t_qty_positive():
    try:
        o.preview({"symbol": "AAPL", "side": "BUY", "quantity": 0, "order_type": "MARKET"})
        raise AssertionError("should reject qty 0")
    except O.OrderError as e:
        assert "quantity" in str(e), e
ok.append(check("rejects quantity 0", t_qty_positive))


def t_limit_needs_price():
    try:
        o.preview({"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT"})
        raise AssertionError("should require limit_price")
    except O.OrderError as e:
        assert "limit_price" in str(e), e
ok.append(check("LIMIT without price rejected", t_limit_needs_price))


def t_bad_side():
    try:
        o.preview({"symbol": "AAPL", "side": "YOLO", "quantity": 1, "order_type": "MARKET"})
        raise AssertionError("should reject side")
    except O.OrderError as e:
        assert "side" in str(e), e
ok.append(check("bogus side rejected", t_bad_side))


def t_market_priced_from_quote():
    # MARKET has no limit price; notional must fall back to the live/last price
    # (stub portfolio says AAPL=188), so 100 shares must still trip the cap.
    try:
        o.preview({"symbol": "AAPL", "side": "BUY", "quantity": 100, "order_type": "MARKET"})
        raise AssertionError("market order should be priced and capped")
    except O.OrderError as e:
        assert "MAX_NOTIONAL" in str(e), e
ok.append(check("MARKET order priced from last for the cap", t_market_priced_from_quote))

print("\n== ticket flow ==")


def t_ticket_roundtrip():
    t = o.preview({"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT",
                   "limit_price": 188})
    assert t["ticket_id"] and t["summary"].startswith("BUY 1 AAPL @ 188"), t
    assert t["origin"] == "ui" and not t["used"], t
    res = o.place_ticket(t["ticket_id"])
    assert res["ok"] and res["account_id"] == "ACC1", res
    assert "portfolio" in wbs.invalidated, wbs.invalidated
ok.append(check("preview -> place by ticket", t_ticket_roundtrip))


def t_ticket_single_use():
    t = o.preview({"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT",
                   "limit_price": 100})
    o.place_ticket(t["ticket_id"])
    try:
        o.place_ticket(t["ticket_id"])
        raise AssertionError("ticket should be single-use")
    except O.OrderError as e:
        assert "already placed" in str(e), e
ok.append(check("ticket cannot be replayed", t_ticket_single_use))


def t_unknown_ticket():
    try:
        o.place_ticket("deadbeef")
        raise AssertionError("should reject unknown ticket")
    except O.OrderError as e:
        assert "unknown" in str(e), e
ok.append(check("unknown ticket rejected", t_unknown_ticket))


def t_direct_blocked():
    try:
        o.place_direct({"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT",
                        "limit_price": 100})
        raise AssertionError("direct place should require confirm off")
    except O.OrderError as e:
        assert "confirmation required" in str(e), e
ok.append(check("place_direct blocked while confirm is on", t_direct_blocked))


def t_summary_bracket():
    t = o.preview({"symbol": "F", "side": "BUY", "quantity": 1, "order_type": "LIMIT",
                   "limit_price": 10.5, "bracket": {"take_profit": 11.5, "stop_loss": 10}})
    assert "TP 11.5" in t["summary"] and "SL 10" in t["summary"], t["summary"]
ok.append(check("bracket summary shows TP/SL", t_summary_bracket))


def t_broker_rejection():
    class Bad(StubOrderOps):
        def place_order(self, aid, payload):
            r = Res({"msg": "insufficient buying power"}); r.status_code = 400; return r
    o2 = O.Orders(StubWebull())
    o2._t.order_v3 = Bad()
    t = o2.preview({"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT",
                    "limit_price": 100})
    try:
        o2.place_ticket(t["ticket_id"])
        raise AssertionError("should surface broker rejection")
    except O.OrderError as e:
        assert "insufficient buying power" in str(e), e
    # and the ticket must be reusable after a broker-side failure
    assert not o2._tickets[t["ticket_id"]].used, "ticket should be reusable after rejection"
ok.append(check("broker rejection surfaced, ticket reusable", t_broker_rejection))

print("\n== cancel/replace ==")


def t_cancel_not_capped():
    o3 = O.Orders(StubWebull())
    r = o3.cancel("ACC1", "coid123")
    assert r["ok"], r
ok.append(check("cancel is never capped", t_cancel_not_capped))


def t_replace_guarded():
    o3 = O.Orders(StubWebull())
    try:
        o3.replace("ACC1", "coid", {"symbol": "AAPL", "quantity": 500, "limit_price": 188})
        raise AssertionError("replace should re-run the cap")
    except O.OrderError as e:
        assert "MAX_NOTIONAL" in str(e), e
ok.append(check("replace re-runs the notional cap", t_replace_guarded))


def t_replace_noop():
    o3 = O.Orders(StubWebull())
    try:
        o3.replace("ACC1", "coid", {})
        raise AssertionError("empty replace should be rejected")
    except O.OrderError as e:
        assert "nothing to change" in str(e), e
ok.append(check("empty replace rejected", t_replace_noop))

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
