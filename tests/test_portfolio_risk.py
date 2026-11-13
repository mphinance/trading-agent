"""core.portfolio_risk: hand-computed expectations, no fixtures."""

import pytest

from core.portfolio_risk import assignment_collateral, build, concentration, leverage, pro_forma


def test_leverage_margin_debit_is_not_cash():
    # $90k of stock financed by a $40k debit: net 50k, 1.8x, 44.4% financed.
    r = leverage(equity_value=90_000.0, cash=-40_000.0)
    assert r["net_equity_usd"] == 50_000.0
    assert r["margin_debit_usd"] == 40_000.0
    assert r["leverage_x"] == pytest.approx(1.8)
    assert r["financed_pct"] == pytest.approx(40_000 / 90_000 * 100)


def test_leverage_unlevered_has_no_debit():
    r = leverage(10_000.0, 500.0)
    assert r["margin_debit_usd"] == 0.0 and r["leverage_x"] == pytest.approx(10_000 / 10_500)


def test_concentration_flags_big_name():
    rows = concentration(
        [{"symbol": "BIG", "shares": 100, "spot": 700.0},
         {"symbol": "SML", "shares": 10, "spot": 100.0}],
        net_equity=50_000.0, gross_exposure=71_000.0,
    )
    assert rows[0]["symbol"] == "BIG" and rows[0]["flagged"]
    assert rows[0]["pct_of_net_equity"] == pytest.approx(140.0)
    assert not rows[1]["flagged"]


def test_put_assignment_keeps_extrinsic_only():
    # K=100 put, spot 91.40, marked 11.70: intrinsic 8.60, extrinsic 3.10.
    base = {"equity_value": 0.0, "cash": 0.0, "options_value": -1170.0}
    ev = {"symbol": "X", "action": "put_assigned", "strike": 100.0, "contracts": 1,
          "option_liability_usd": 1170.0, "mark": 91.40}
    r = pro_forma(base, [ev])
    assert r["net_equity_usd"] - (-1170.0) == pytest.approx(310.0)
    assert r["gross_exposure_usd"] == pytest.approx(9140.0)


def test_collateral_shortfall_only_when_unreserved():
    put = {"symbol": "X", "strike": 50.0, "spot": 45.0, "dte": 3, "contracts": 2, "mark": 6.0}
    # Robinhood reserves CSP collateral at open: buying power is already net of it.
    reserved = assignment_collateral([put], buying_power=1_000.0)
    assert reserved["covered_by_buying_power"] and reserved["shortfall_usd"] == 0.0
    # An explicitly unreserved put needs 50*100*2 = 10,000 against 1,000.
    open_ = assignment_collateral([dict(put, collateral_reserved=False)], buying_power=1_000.0)
    assert not open_["covered_by_buying_power"]
    assert open_["shortfall_usd"] == pytest.approx(9_000.0)


def test_build_warns_on_leverage_and_assignment():
    payload = {
        "equity_value": 90_000.0, "cash": -40_000.0, "options_value": -600.0,
        "buying_power": 500.0, "max_account_leverage_x": 1.9,
        "positions": [{"symbol": "BIG", "shares": 100, "spot": 700.0}],
        "short_puts": [{"symbol": "X", "strike": 50.0, "spot": 45.0, "dte": 3,
                        "contracts": 1, "mark": 6.0}],
    }
    r = build(payload)
    text = " ".join(r["warnings"])
    assert "levered" in text and "BIG" in text and "assigned" in text
