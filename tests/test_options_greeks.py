"""Tests for core/options_greeks.py's Black-Scholes greeks.

Ported from a friend's discount-bloomberg repo (analytics/greeks.py), whose test suite caught
two real sign bugs in the reference implementation it was modeled on. The finite-difference
checks are the ones that matter: they pin each greek against a central difference of the
function it is the derivative of, so a sign or scaling error can't hide behind a single
memorized value.
"""

from __future__ import annotations

import math

import pytest

from core import options_greeks as g

# At-the-money, one year, 5% rates. Values from any standard BSM reference (Hull's worked
# examples).
ATM = dict(S=100.0, K=100.0, T=1.0, r=0.05, sigma=0.20)

# Off-centre contract for the derivative checks — OTM, half a year — so no term drops out by
# symmetry and a sign error cannot hide.
SKEW = dict(S=100.0, K=105.0, T=0.5, r=0.04, sigma=0.30)


# --------------------------------------------------------------------------- known values


def test_textbook_call_price() -> None:
    assert g.bs_call_price(**ATM) == pytest.approx(10.450584, abs=1e-5)


def test_textbook_put_price() -> None:
    assert g.bs_put_price(**ATM) == pytest.approx(5.573526, abs=1e-5)


def test_put_call_parity() -> None:
    """C - P == S - K*e^-rT (no dividend yield anywhere in this module)."""
    c = g.bs_call_price(**ATM)
    p = g.bs_put_price(**ATM)
    expected = ATM["S"] - ATM["K"] * math.exp(-ATM["r"] * ATM["T"])
    assert c - p == pytest.approx(expected, abs=1e-9)


def test_delta_parity() -> None:
    """delta_call - delta_put == 1 (no dividend yield)."""
    c = g.bs_delta(**SKEW, is_put=False)
    p = g.bs_delta(**SKEW, is_put=True)
    assert c - p == pytest.approx(1.0, abs=1e-9)


# ------------------------------------------------------------------- finite differences


def _central(fn, var: str, step: float, **fixed) -> float:
    hi = fn(**{**SKEW, **fixed, var: SKEW[var] + step})
    lo = fn(**{**SKEW, **fixed, var: SKEW[var] - step})
    return (hi - lo) / (2.0 * step)


@pytest.mark.parametrize("is_put", [True, False])
def test_delta_is_derivative_of_price(is_put: bool) -> None:
    price_fn = g.bs_put_price if is_put else g.bs_call_price
    analytic = g.bs_delta(**SKEW, is_put=is_put)
    numeric = _central(price_fn, "S", 1e-4)
    assert analytic == pytest.approx(numeric, rel=1e-4)


def test_gamma_is_derivative_of_delta() -> None:
    """Delta's slope is identical for a call and put at the same strike (they differ by a
    constant, 1.0), so gamma needs only one side checked."""
    analytic = g.bs_gamma(**SKEW)
    numeric = _central(g.bs_delta, "S", 1e-4, is_put=False)
    assert analytic == pytest.approx(numeric, rel=1e-4)


def test_vega_is_derivative_of_price() -> None:
    analytic = g.bs_vega(**SKEW)
    numeric = _central(g.bs_call_price, "sigma", 1e-6) / 100.0  # bs_vega is scaled per vol-point
    assert analytic == pytest.approx(numeric, rel=1e-4)


def test_vanna_is_derivative_of_delta_wrt_vol() -> None:
    analytic = g.bs_vanna(**SKEW)
    numeric = _central(g.bs_delta, "sigma", 1e-5, is_put=False)
    assert analytic == pytest.approx(numeric, rel=1e-4)


def test_vomma_is_derivative_of_vega_wrt_vol() -> None:
    analytic = g.bs_vomma(**SKEW)
    numeric = _central(g.bs_vega, "sigma", 1e-5)
    assert analytic == pytest.approx(numeric, rel=1e-4)


@pytest.mark.parametrize("is_put", [True, False])
def test_theta_is_negative_derivative_of_price_wrt_time(is_put: bool) -> None:
    """theta reads as -d(price)/dT (value bled by calendar time PASSING, i.e. T shrinking)."""
    price_fn = g.bs_put_price if is_put else g.bs_call_price
    analytic = g.bs_theta(**SKEW, is_put=is_put) * 365.0  # undo the daily scaling to compare raw
    numeric = -_central(price_fn, "T", 1e-6)
    assert analytic == pytest.approx(numeric, rel=1e-4)


def test_charm_is_negative_derivative_of_delta_wrt_time() -> None:
    analytic = g.bs_charm(**SKEW) * 365.0  # undo the daily scaling to compare raw
    numeric = -_central(g.bs_delta, "T", 1e-6, is_put=False)
    assert analytic == pytest.approx(numeric, rel=1e-4)


# -------------------------------------------------------------------------- implied vol


@pytest.mark.parametrize("is_put", [True, False])
@pytest.mark.parametrize("sigma", [0.05, 0.20, 0.65, 1.50])
def test_implied_vol_round_trip(is_put: bool, sigma: float) -> None:
    """Price at a known vol, solve it back. This is the check the reference's solver failed."""
    params = {**SKEW, "sigma": sigma}
    price_fn = g.bs_put_price if is_put else g.bs_call_price
    px = price_fn(**params)
    solved = g.implied_vol(px, SKEW["S"], SKEW["K"], SKEW["T"], SKEW["r"], is_put=is_put)
    assert solved == pytest.approx(sigma, abs=1e-6)


def test_implied_vol_returns_none_below_intrinsic() -> None:
    intrinsic = 200.0 - 100.0
    assert g.implied_vol(intrinsic * 0.5, 200.0, 100.0, 0.5, 0.04, is_put=False) is None


def test_implied_vol_returns_none_above_ceiling() -> None:
    """A call cannot be worth more than the underlying."""
    assert g.implied_vol(150.0, 100.0, 100.0, 0.5, 0.04, is_put=False) is None


def test_implied_vol_rejects_nonpositive_price() -> None:
    assert g.implied_vol(0.0, 100.0, 100.0, 0.5, 0.04, is_put=False) is None
    assert g.implied_vol(-1.0, 100.0, 100.0, 0.5, 0.04, is_put=False) is None


# ------------------------------------------------------------------------- degenerate inputs


def test_expired_contract_returns_none_not_a_crash() -> None:
    """T=0 hits the T<=0 guard in _d1_d2 rather than dividing by zero."""
    assert g.bs_call_price(S=100.0, K=100.0, T=0.0, r=0.05, sigma=0.30) is None
    assert g.bs_gamma(S=100.0, K=100.0, T=0.0, r=0.05, sigma=0.30) is None


def test_zero_vol_returns_none_not_a_crash() -> None:
    assert g.bs_call_price(S=100.0, K=100.0, T=1.0, r=0.05, sigma=0.0) is None
