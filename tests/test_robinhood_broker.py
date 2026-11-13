"""Robinhood adapter: the fences hold with a fake transport (no network)."""

import pytest

from vesper.brokers.robinhood_broker import RobinhoodBrokerClient, RobinhoodError

ACCTS = {"accounts": [
    {"account_number": "AGENTIC1", "agentic_allowed": True},
    {"account_number": "LONGTERM", "agentic_allowed": False},
]}


class Fake:
    def __init__(self, approval=False, block=False):
        self.block = block
        self.calls = []
        self.approval = approval

    def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "get_accounts":
            return ACCTS
        if name == "get_portfolio":
            return {"buying_power": {"buying_power": "712.81"}}
        if name == "review_equity_order" and self.block:
            return {"order_checks": {"alertType": "EQUITY_NOT_ENOUGH_BP"}}
        if name == "place_equity_order":
            return {"order_id": "o1", "customer_approval_required": self.approval}
        return {"ok": True}


def client(acct="AGENTIC1", **kw):
    f = Fake(**kw)
    return RobinhoodBrokerClient(account_id=acct, transport=f), f


def placed(f):
    return [c for c in f.calls if c[0] == "place_equity_order"]


def test_refuses_non_agentic_account():
    c, f = client("LONGTERM")
    with pytest.raises(RobinhoodError, match="not an agentic"):
        c.place_order("AAPL", "buy", 1, "LIMIT", 100.0)
    assert not placed(f)


def test_refuses_unknown_account():
    c, f = client("NOPE")
    with pytest.raises(RobinhoodError):
        c.preview_order("AAPL", "buy", 1, "LIMIT", 100.0)
    assert not placed(f)


def test_refuses_options():
    c, f = client()
    with pytest.raises(RobinhoodError, match="equities only"):
        c.place_order("AAPL", "buy", 1, "LIMIT", 5.0, asset_type="OPTION")
    assert not placed(f)


def test_unconfigured_without_account():
    assert not RobinhoodBrokerClient(account_id="", transport=Fake()).configured


def test_buying_power_parsed():
    c, _ = client()
    assert c.get_buying_power() == 712.81


def test_limit_order_wire_format_and_idempotency_key():
    c, f = client()
    c.place_order("aapl", "BUY", 3, "LIMIT", 187.5, ref_id="r-1")
    (_, args), = placed(f)
    assert args == {"account_number": "AGENTIC1", "ref_id": "r-1", "symbol": "AAPL",
                    "side": "buy", "type": "limit", "quantity": "3",
                    "time_in_force": "gfd", "limit_price": "187.50"}


def test_limit_without_price_rejected():
    c, f = client()
    with pytest.raises(RobinhoodError):
        c.place_order("AAPL", "buy", 1, "LIMIT", None)
    assert not placed(f)


def test_robinhood_app_approval_is_not_reported_as_fill():
    c, _ = client(approval=True)
    assert c.place_order("AAPL", "buy", 1, "LIMIT", 100.0)["status"] == "pending_robinhood_approval"


def test_preview_places_nothing():
    c, f = client()
    c.preview_order("AAPL", "buy", 1, "LIMIT", 100.0)
    assert not placed(f) and any(n == "review_equity_order" for n, _ in f.calls)


def test_broker_pre_trade_alert_blocks_placement():
    c, f = client(block=True)
    with pytest.raises(RobinhoodError, match="pre-trade check"):
        c.place_order("AAPL", "buy", 1, "LIMIT", 100.0)
    assert not placed(f)
