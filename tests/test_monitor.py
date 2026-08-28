"""Unit tests for Active Position Monitor & Exit Cascade Loop (Module 3)."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from vesper.monitor import PositionMonitor, MonitoredPosition, ExitTrigger


def test_take_profit_trigger():
    monitor = PositionMonitor()
    # Entry $100 -> Current $155 (+55% gain)
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=155.0)
    trigger = monitor.evaluate_position(pos)
    assert trigger is not None
    assert trigger.reason == "TAKE_PROFIT"
    assert trigger.sell_quantity == 10
    assert trigger.est_proceeds == 1550.0
    assert trigger.pnl_pct == 0.55


def test_stop_loss_trigger():
    monitor = PositionMonitor()
    # Entry $100 -> Current $55 (-45% loss)
    pos = MonitoredPosition(symbol="TSLA", quantity=5, entry_price=100.0, current_price=55.0)
    trigger = monitor.evaluate_position(pos)
    assert trigger is not None
    assert trigger.reason == "STOP_LOSS"
    assert trigger.urgency == "CRITICAL"
    assert trigger.pnl_pct == -0.45


def test_trailing_breakeven_lock_and_stop():
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="AMD", quantity=10, entry_price=100.0, current_price=128.0)
    # First tick: +28% gain -> Should activate breakeven lock, but NOT trigger exit
    trigger1 = monitor.evaluate_position(pos)
    assert trigger1 is None
    assert pos.breakeven_locked is True

    # Second tick: Price drops back to $99 (-1%) -> Should trigger BREAKEVEN_STOP
    pos.current_price = 99.0
    trigger2 = monitor.evaluate_position(pos)
    assert trigger2 is not None
    assert trigger2.reason == "BREAKEVEN_STOP"
    assert trigger2.sell_quantity == 10


def test_0dte_time_stop_trigger():
    monitor = PositionMonitor()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pos = MonitoredPosition(
        symbol="SPY",
        quantity=2,
        entry_price=2.00,
        current_price=2.10,
        asset_type="OPTION",
        expiry=today_str,
    )
    assert pos.is_0dte is True

    # 14:30 ET -> Within trading window, no time stop
    time_early = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
    trigger_early = monitor.evaluate_position(pos, current_time_et=time_early)
    assert trigger_early is None

    # 15:05 ET -> Exceeded 15:00 ET cutoff -> Triggers TIME_STOP
    time_late = datetime(2026, 8, 28, 15, 5, tzinfo=timezone.utc)
    trigger_late = monitor.evaluate_position(pos, current_time_et=time_late)
    assert trigger_late is not None
    assert trigger_late.reason == "TIME_STOP"
    assert trigger_late.sell_quantity == 2


def test_gamma_flip_violation_trigger():
    monitor = PositionMonitor()
    # SPY Call bought at $3.00 premium, currently $2.80 (-6.6% loss), SPY spot is $570, but Gamma Flip is at $575 -> Violation
    pos = MonitoredPosition(
        symbol="SPY",
        quantity=1,
        entry_price=3.00,
        current_price=2.80,
        asset_type="OPTION",
        option_type="CALL",
    )
    trigger = monitor.evaluate_position(pos, spy_spot=570.0, spy_gamma_flip=575.0)
    assert trigger is not None
    assert trigger.reason == "GAMMA_FLIP_VIOLATION"


def test_healthy_position_no_trigger():
    monitor = PositionMonitor()
    # +10% gain -> Healthy, no trigger
    pos = MonitoredPosition(symbol="AAPL", quantity=20, entry_price=200.0, current_price=220.0)
    trigger = monitor.evaluate_position(pos)
    assert trigger is None


@pytest.mark.asyncio
async def test_execute_exit_cascade_dry_run():
    monitor = PositionMonitor()
    pos = MonitoredPosition(symbol="NVDA", quantity=10, entry_price=100.0, current_price=160.0)
    trigger = ExitTrigger(
        position=pos,
        reason="TAKE_PROFIT",
        sell_quantity=10,
        est_proceeds=1600.0,
        pnl_pct=0.60,
    )
    res = await monitor.execute_exit_cascade(trigger, live=False)
    assert res.status == "DRY_RUN_SIMULATED"
    assert res.ticker == "NVDA"
    assert res.filled_quantity == 10
    assert res.filled_price == 160.0
