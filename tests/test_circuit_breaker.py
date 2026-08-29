"""Unit tests for the portfolio drawdown circuit breaker."""

from __future__ import annotations

import pytest

from vesper.circuit_breaker import check_portfolio_drawdown, get_peak_nlv
from vesper.halt import is_halted, resume


@pytest.fixture(autouse=True)
def clean_breaker_state(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("vesper.circuit_breaker._DATA_DIR", data_dir)
    monkeypatch.setattr("vesper.circuit_breaker._STATE_PATH", data_dir / "circuit_breaker_state.json")
    monkeypatch.setattr("vesper.halt._DATA_DIR", data_dir)
    monkeypatch.setattr("vesper.halt._HALT_STATE_PATH", data_dir / "halt_state.json")
    yield


def test_first_call_establishes_peak_no_trip():
    res = check_portfolio_drawdown(100_000.0)
    assert res["peak_nlv"] == 100_000.0
    assert res["drawdown_pct"] == 0.0
    assert res["tripped_now"] is False
    assert not is_halted()[0]


def test_new_high_raises_peak():
    check_portfolio_drawdown(100_000.0)
    res = check_portfolio_drawdown(110_000.0)
    assert res["peak_nlv"] == 110_000.0
    assert get_peak_nlv() == 110_000.0


def test_drawdown_below_threshold_does_not_trip():
    check_portfolio_drawdown(100_000.0)
    res = check_portfolio_drawdown(90_000.0)  # -10%, below the 15% default
    assert res["drawdown_pct"] == pytest.approx(0.10)
    assert res["tripped_now"] is False
    assert not is_halted()[0]


def test_drawdown_at_or_above_threshold_trips_halt():
    check_portfolio_drawdown(100_000.0)
    res = check_portfolio_drawdown(84_000.0)  # -16%
    assert res["tripped_now"] is True
    halted, info = is_halted()
    assert halted
    assert "circuit breaker" in info["reason"].lower()
    assert info["halted_by"] == "circuit_breaker"


def test_already_halted_does_not_retrip_or_overwrite_reason():
    check_portfolio_drawdown(100_000.0)
    check_portfolio_drawdown(80_000.0)  # trips
    halted, info_first = is_halted()
    assert halted

    # Further degradation while already halted must not re-trip or touch the halt record.
    res2 = check_portfolio_drawdown(50_000.0)
    assert res2["tripped_now"] is False
    _, info_second = is_halted()
    assert info_second["halted_at"] == info_first["halted_at"]


def test_resume_starts_a_fresh_peak_instead_of_immediately_retripping():
    check_portfolio_drawdown(100_000.0)
    check_portfolio_drawdown(80_000.0)  # trips at peak=100k
    assert is_halted()[0]

    resume(source="test")
    assert not is_halted()[0]

    # Still technically 20% below the OLD peak (100k) -- must NOT re-trip,
    # because the fresh-peak reset means this current NLV becomes the new peak.
    res = check_portfolio_drawdown(80_000.0)
    assert res["tripped_now"] is False
    assert res["peak_nlv"] == 80_000.0
    assert not is_halted()[0]

    # Now a real 15%+ drop FROM the new peak should trip again.
    res2 = check_portfolio_drawdown(67_000.0)  # -16.25% from 80k
    assert res2["tripped_now"] is True
    assert is_halted()[0]


def test_zero_or_negative_nlv_is_skipped_not_treated_as_total_loss():
    check_portfolio_drawdown(100_000.0)
    res = check_portfolio_drawdown(0.0)
    assert res.get("skipped") is True
    assert not is_halted()[0]
    assert get_peak_nlv() == 100_000.0  # peak untouched

    res_neg = check_portfolio_drawdown(-500.0)
    assert res_neg.get("skipped") is True
    assert not is_halted()[0]


def test_custom_threshold_respected():
    check_portfolio_drawdown(100_000.0, threshold_pct=0.05)
    res = check_portfolio_drawdown(94_000.0, threshold_pct=0.05)  # -6%
    assert res["tripped_now"] is True
