"""Continuous Execution Daemon (`vesper loop`).

Unattended market-hours scheduling: fires a full scan pass
(vesper.runner.run_agent_session) at a handful of scheduled ET times, and
runs monitor.py's position-monitor loop continuously in the background
between them. This is the "stays running while you're away" window,
complementing `vesper listen` (Telegram approval polling) -- run as its own
separate process, same "three windows" pattern as the rest of this repo.

Safety property this is built around: mode="dry_run" runs fully
autonomously (interactive=False -> human_gate_node auto-simulates, can
never reach a live broker, matches AUTO_DRY_RUN's existing semantics).
mode="live" always runs interactive=True instead -- a scheduled scan can
DRAFT proposals, but human_gate_node's interrupt() pauses the graph and
waits for an explicit Telegram/Discord approval tap before anything
executes. There is no code path here where "live" and "unattended" combine
to place an order without a human in the loop.

Respects halt(): a scheduled scan is skipped (not run at all, not silently
queued) while halted -- running a full pass while frozen is wasteful and
semantically wrong even though execution_guard would block any live order
regardless.

No holiday calendar. Weekends are skipped outright; a scheduled scan firing
on a market holiday will mostly draft nothing (every playbook's own
"skip rather than fabricate" quote-fetch paths cover live-data
unavailability) -- annoying, not unsafe. A real holiday calendar is future
work, not required to ship this safely.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from datetime import time as dt_time
from typing import Optional, Set, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Scheduled full-scan times, ET. 15:00 (the 0DTE hard exit time) is
# deliberately NOT a scan slot here -- that's monitor.py's own time-based
# exit rule (RiskEnforcer.HARD_EXIT_TIME_0DTE), already running continuously
# via the background monitor task, not a new candidate-drafting pass.
DEFAULT_SCAN_TIMES: Tuple[dt_time, ...] = (dt_time(9, 30), dt_time(11, 0), dt_time(14, 0))

DEFAULT_POLL_INTERVAL_SEC = 30.0
DEFAULT_MONITOR_INTERVAL_SEC = 15.0
# How late past a scheduled time this process will still fire it (once) if
# it wasn't running exactly on time. Wider than the poll interval so a
# process restart near a scheduled time doesn't miss it, narrow enough that
# a process that was down for hours doesn't fire every missed slot at once.
CATCH_UP_WINDOW_SEC = 300.0

ScanSlotKey = Tuple[date, dt_time]


def _should_fire_scan(
    now_et: datetime,
    scheduled_time: dt_time,
    already_fired_today: Set[ScanSlotKey],
    catch_up_window_sec: float = CATCH_UP_WINDOW_SEC,
) -> bool:
    """Pure predicate: should scheduled_time fire right now?

    Weekends are always skipped. A slot fires at most once per calendar day
    (tracked via already_fired_today), and only within catch_up_window_sec
    after its scheduled time -- not before, and not indefinitely after.
    """
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    key: ScanSlotKey = (now_et.date(), scheduled_time)
    if key in already_fired_today:
        return False

    scheduled_dt = datetime.combine(now_et.date(), scheduled_time, tzinfo=now_et.tzinfo)
    elapsed = (now_et - scheduled_dt).total_seconds()
    return 0 <= elapsed <= catch_up_window_sec


async def _check_and_fire_scans(
    now_et: datetime,
    scan_times: Tuple[dt_time, ...],
    already_fired_today: Set[ScanSlotKey],
    mode: str,
    playbook: str,
    persona: str,
) -> Set[ScanSlotKey]:
    """One scheduling tick: fire any due scan slots, return the updated
    already-fired set. Split out from run_continuous_loop's while-True
    wrapper so it's directly testable with an injected now_et, the same
    poll_once/run_forever split telegram_polling.py already uses."""
    from core.halt import is_halted
    from vesper.runner import run_agent_session
    from vesper.bot.manager import channel_manager

    # Prune entries from a previous calendar day so the set doesn't grow
    # forever across a multi-day-running process.
    already_fired_today = {k for k in already_fired_today if k[0] == now_et.date()}

    for scheduled_time in scan_times:
        if not _should_fire_scan(now_et, scheduled_time, already_fired_today):
            continue

        key: ScanSlotKey = (now_et.date(), scheduled_time)
        already_fired_today.add(key)
        slot_label = scheduled_time.strftime("%H:%M ET")

        halted, halt_info = is_halted()
        if halted:
            logger.warning(f"Skipping scheduled scan at {slot_label}: Vesper is halted ({halt_info.get('reason')})")
            if channel_manager.active_channels:
                await channel_manager.broadcast_alert(
                    title="Scheduled Scan Skipped",
                    message=f"Vesper is halted -- skipping the {slot_label} scan. Reason: {halt_info.get('reason')}",
                    level="WARNING",
                )
            continue

        logger.info(f"⏰ Firing scheduled scan for {slot_label} (mode={mode}, playbook={playbook})")
        try:
            # mode="live" always runs interactive so a drafted proposal
            # pauses at human_gate_node for a real Telegram/Discord approval
            # tap -- never auto-executes just because nobody was watching.
            await run_agent_session(
                mode=mode, playbook=playbook, interactive=(mode == "live"), persona=persona,
            )
            if channel_manager.active_channels:
                await channel_manager.broadcast_alert(
                    title="Scheduled Scan Complete",
                    message=f"{slot_label} scan finished (mode={mode}, playbook={playbook}).",
                    level="INFO",
                )
        except Exception as e:
            logger.error(f"Scheduled scan at {slot_label} failed: {e}")
            if channel_manager.active_channels:
                await channel_manager.broadcast_alert(
                    title="Scheduled Scan Failed",
                    message=f"{slot_label} scan raised: {e}",
                    level="ERROR",
                )

    return already_fired_today


async def run_continuous_loop(
    mode: str = "dry_run",
    playbook: str = "all",
    persona: str = "default",
    scan_times: Tuple[dt_time, ...] = DEFAULT_SCAN_TIMES,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    monitor_interval_sec: float = DEFAULT_MONITOR_INTERVAL_SEC,
) -> None:
    """Runs indefinitely: monitor.py's position monitor in the background,
    plus scheduled full scans at scan_times (ET, weekdays only)."""
    from vesper.monitor import run_monitor_loop

    print("\n" + "=" * 76)
    print(f"🔁 VESPER CONTINUOUS LOOP (Mode: {mode.upper()} | Playbook: {playbook.upper()})")
    print(f"Scheduled scans: {', '.join(t.strftime('%H:%M') for t in scan_times)} ET (weekdays)")
    print("Position monitor: running continuously in the background")

    # Alert watcher: armed dealer-gamma/price alerts are evaluated here, in the
    # long-running process, for the same reason watcher.py's own docstring
    # gives -- an alert that only fires while you happen to be asking about it
    # is not an alert. Started best-effort: a broken alert stack must never
    # take down scheduled scans or position monitoring.
    try:
        from vesper.alerts_runner import build_watcher

        w = build_watcher(start=True)
        print(f"Alert watcher: running ({len(w.store.symbols())} symbol(s) armed)")
    except Exception as e:
        logger.error(f"Alert watcher failed to start (continuing without it): {e}")
        print(f"Alert watcher: FAILED TO START — {e}")

    print("=" * 76)

    monitor_task = asyncio.create_task(
        run_monitor_loop(interval_sec=monitor_interval_sec, live=(mode == "live"))
    )

    fired_today: Set[ScanSlotKey] = set()
    try:
        while True:
            now_et = datetime.now(ET)
            try:
                fired_today = await _check_and_fire_scans(
                    now_et, scan_times, fired_today, mode, playbook, persona,
                )
            except Exception as e:
                logger.error(f"Error in continuous loop scheduling tick: {e}")

            # Cross-process metrics surfacing: `vesper status` runs as a
            # fresh one-shot process and can't see this process's in-memory
            # counters, so write them out on the existing poll cadence (same
            # atomic write pattern halt.py's _save_state uses). Best-effort --
            # a metrics-write failure must never take down the loop itself.
            try:
                from vesper.metrics import write_snapshot
                write_snapshot()
            except Exception as e:
                logger.warning(f"Metrics snapshot write failed (continuing): {e}")

            await asyncio.sleep(poll_interval_sec)
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
