"""Unit tests for the continuous execution daemon (vesper loop)."""

from __future__ import annotations

from datetime import datetime, time as dt_time
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from vesper.loop import (
    CATCH_UP_WINDOW_SEC,
    ET,
    _check_and_fire_scans,
    _should_fire_scan,
)

MON_930 = datetime(2026, 8, 24, 9, 30, 0, tzinfo=ET)  # a Monday
MON_1100 = datetime(2026, 8, 24, 11, 0, 0, tzinfo=ET)
SAT_930 = datetime(2026, 8, 22, 9, 30, 0, tzinfo=ET)  # a Saturday
SUN_930 = datetime(2026, 8, 23, 9, 30, 0, tzinfo=ET)  # a Sunday


# ── _should_fire_scan (pure predicate) ───────────────────────────────────────

def test_fires_exactly_at_scheduled_time():
    assert _should_fire_scan(MON_930, dt_time(9, 30), set())


def test_fires_within_catch_up_window_after_scheduled_time():
    late = MON_930.replace(minute=32)  # 2 min late, well within the 5-min window
    assert _should_fire_scan(late, dt_time(9, 30), set())


def test_does_not_fire_before_scheduled_time():
    early = MON_930.replace(minute=29)
    assert not _should_fire_scan(early, dt_time(9, 30), set())


def test_does_not_fire_past_catch_up_window():
    too_late = MON_930.replace(hour=9, minute=30 + int(CATCH_UP_WINDOW_SEC // 60) + 5)
    assert not _should_fire_scan(too_late, dt_time(9, 30), set())


def test_does_not_refire_same_slot_same_day():
    already_fired = {(MON_930.date(), dt_time(9, 30))}
    assert not _should_fire_scan(MON_930, dt_time(9, 30), already_fired)


def test_refires_same_slot_next_day():
    already_fired = {(MON_930.date(), dt_time(9, 30))}
    tuesday_930 = MON_930.replace(day=25)
    assert _should_fire_scan(tuesday_930, dt_time(9, 30), already_fired)


@pytest.mark.parametrize("weekend_dt", [SAT_930, SUN_930])
def test_never_fires_on_weekends(weekend_dt):
    assert not _should_fire_scan(weekend_dt, dt_time(9, 30), set())


# ── _check_and_fire_scans (one scheduling tick) ──────────────────────────────

@pytest.mark.asyncio
async def test_tick_fires_due_slot_and_marks_it_fired():
    with patch("vesper.halt.is_halted", return_value=(False, None)):
        with patch("vesper.runner.run_agent_session", new_callable=AsyncMock) as mock_session:
            with patch("vesper.bot.manager.channel_manager") as mock_cm:
                mock_cm.active_channels = []
                fired = await _check_and_fire_scans(
                    MON_930, (dt_time(9, 30), dt_time(11, 0)), set(),
                    mode="dry_run", playbook="all", persona="default",
                )

    mock_session.assert_called_once_with(
        mode="dry_run", playbook="all", interactive=False, persona="default",
    )
    assert (MON_930.date(), dt_time(9, 30)) in fired
    assert (MON_930.date(), dt_time(11, 0)) not in fired  # not due yet


@pytest.mark.asyncio
async def test_tick_skips_when_halted_and_does_not_call_run_agent_session():
    halt_info = {"reason": "manual freeze", "halted_by": "cli"}
    with patch("vesper.halt.is_halted", return_value=(True, halt_info)):
        with patch("vesper.runner.run_agent_session", new_callable=AsyncMock) as mock_session:
            with patch("vesper.bot.manager.channel_manager") as mock_cm:
                mock_cm.active_channels = []
                fired = await _check_and_fire_scans(
                    MON_930, (dt_time(9, 30),), set(),
                    mode="dry_run", playbook="all", persona="default",
                )

    mock_session.assert_not_called()
    # Still marked fired for today -- a halted slot isn't retried every tick
    # until it un-halts; it's simply skipped for that occurrence.
    assert (MON_930.date(), dt_time(9, 30)) in fired


@pytest.mark.asyncio
async def test_tick_live_mode_runs_interactive_true():
    """A live-mode scheduled scan must pause for remote approval, never
    auto-execute just because nobody was watching."""
    with patch("vesper.halt.is_halted", return_value=(False, None)):
        with patch("vesper.runner.run_agent_session", new_callable=AsyncMock) as mock_session:
            with patch("vesper.bot.manager.channel_manager") as mock_cm:
                mock_cm.active_channels = []
                await _check_and_fire_scans(
                    MON_930, (dt_time(9, 30),), set(),
                    mode="live", playbook="all", persona="default",
                )

    mock_session.assert_called_once_with(
        mode="live", playbook="all", interactive=True, persona="default",
    )


@pytest.mark.asyncio
async def test_tick_broadcasts_completion_when_channels_active():
    with patch("vesper.halt.is_halted", return_value=(False, None)):
        with patch("vesper.runner.run_agent_session", new_callable=AsyncMock):
            with patch("vesper.bot.manager.channel_manager") as mock_cm:
                mock_cm.active_channels = ["telegram"]
                mock_cm.broadcast_alert = AsyncMock()
                await _check_and_fire_scans(
                    MON_930, (dt_time(9, 30),), set(),
                    mode="dry_run", playbook="all", persona="default",
                )

    mock_cm.broadcast_alert.assert_called_once()
    _, kwargs = mock_cm.broadcast_alert.call_args
    assert kwargs["level"] == "INFO"


@pytest.mark.asyncio
async def test_tick_swallows_scan_exception_and_broadcasts_failure():
    with patch("vesper.halt.is_halted", return_value=(False, None)):
        with patch("vesper.runner.run_agent_session", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            with patch("vesper.bot.manager.channel_manager") as mock_cm:
                mock_cm.active_channels = ["telegram"]
                mock_cm.broadcast_alert = AsyncMock()
                # Must not raise -- one bad scan shouldn't kill the loop.
                fired = await _check_and_fire_scans(
                    MON_930, (dt_time(9, 30),), set(),
                    mode="dry_run", playbook="all", persona="default",
                )

    assert (MON_930.date(), dt_time(9, 30)) in fired
    mock_cm.broadcast_alert.assert_called_once()
    _, kwargs = mock_cm.broadcast_alert.call_args
    assert kwargs["level"] == "ERROR"


@pytest.mark.asyncio
async def test_tick_prunes_stale_dates_from_fired_set():
    yesterday_key = (MON_930.date().replace(day=MON_930.day - 1), dt_time(9, 30))
    with patch("vesper.halt.is_halted", return_value=(False, None)):
        with patch("vesper.runner.run_agent_session", new_callable=AsyncMock):
            with patch("vesper.bot.manager.channel_manager") as mock_cm:
                mock_cm.active_channels = []
                fired = await _check_and_fire_scans(
                    MON_930, (dt_time(11, 0),), {yesterday_key},
                    mode="dry_run", playbook="all", persona="default",
                )

    assert yesterday_key not in fired
