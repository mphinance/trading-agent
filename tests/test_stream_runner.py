"""Trade-event push -> immediate monitor wake-up.

The property that matters: a fill must not have to wait out the poll interval.
Before stream.py was restored, monitor.py slept a flat 15s between cycles while
enforcing a -40% stop on 0DTE positions, so every exit decision could be made
against data up to a full interval stale.

Equally important is the degradation: if the feed can't start (no SDK, blocked
egress, no credentials), the monitor must behave exactly as it did before
rather than raise.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

import vesper.stream_runner as sr


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """stream_runner keeps process-lifetime singletons; reset per test."""
    monkeypatch.setattr(sr, "_events", None)
    monkeypatch.setattr(sr, "_consumer_task", None)


@pytest.mark.asyncio
async def test_returns_false_and_does_not_raise_when_webull_unconfigured():
    with patch("core.wb.Webull", return_value=MagicMock(configured=False)):
        assert await sr.start_trade_events(asyncio.Event()) is False


@pytest.mark.asyncio
async def test_returns_false_and_does_not_raise_when_stream_import_explodes():
    """Blocked egress / missing SDK must degrade, never propagate."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def boom(name, *a, **k):
        if name == "stream":
            raise ImportError("no grpc egress")
        return real_import(name, *a, **k)

    with patch("builtins.__import__", side_effect=boom):
        assert await sr.start_trade_events(asyncio.Event()) is False


@pytest.mark.asyncio
async def test_returns_false_when_account_has_no_ids():
    wb = MagicMock(configured=True)
    wb.account_ids.return_value = []
    with patch("core.wb.Webull", return_value=wb), \
         patch("core.wb.credentials", return_value=("k", "s", "us")):
        assert await sr.start_trade_events(asyncio.Event()) is False


async def _start_with_fake_bus(wake, queue):
    """Start the feed with stream.bus/EventStream faked out."""
    wb = MagicMock(configured=True)
    wb.account_ids.return_value = ["acct-1"]
    fake_stream = MagicMock()
    fake_stream.bus.subscribe.return_value = queue
    fake_stream.EventStream.return_value = MagicMock(connected=True, error=None)

    with patch.dict("sys.modules", {"stream": fake_stream}), \
         patch("core.wb.Webull", return_value=wb), \
         patch("core.wb.credentials", return_value=("k", "s", "us")):
        ok = await sr.start_trade_events(wake)
    return ok


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["order", "position", "option"])
async def test_position_changing_event_sets_the_wake_event(kind):
    wake, queue = asyncio.Event(), asyncio.Queue()
    assert await _start_with_fake_bus(wake, queue) is True

    await queue.put({"type": "event", "kind": kind, "payload": {}})
    # The wake must arrive far inside a normal 15s poll interval.
    await asyncio.wait_for(wake.wait(), timeout=2.0)
    assert wake.is_set()

    await sr.stop_trade_events()


@pytest.mark.asyncio
async def test_unrelated_events_do_not_wake_the_monitor():
    """A connect/disconnect notice changes no position — waking on it would
    just burn the account-query rate-limit bucket for nothing."""
    wake, queue = asyncio.Event(), asyncio.Queue()
    assert await _start_with_fake_bus(wake, queue) is True

    await queue.put({"type": "stream", "feed": "events", "state": "connected"})
    await queue.put({"type": "event", "kind": "heartbeat", "payload": {}})
    await asyncio.sleep(0.2)
    assert not wake.is_set()

    await sr.stop_trade_events()


@pytest.mark.asyncio
async def test_malformed_events_are_ignored_not_raised():
    wake, queue = asyncio.Event(), asyncio.Queue()
    assert await _start_with_fake_bus(wake, queue) is True

    for junk in ["not a dict", None, 42, {"no_type": True}]:
        await queue.put(junk)
    await asyncio.sleep(0.2)
    assert not wake.is_set()

    # still alive and still able to wake on a real event afterwards
    await queue.put({"type": "event", "kind": "order", "payload": {}})
    await asyncio.wait_for(wake.wait(), timeout=2.0)

    await sr.stop_trade_events()


@pytest.mark.asyncio
async def test_start_is_idempotent():
    wake, queue = asyncio.Event(), asyncio.Queue()
    assert await _start_with_fake_bus(wake, queue) is True
    # second call must not build a second feed/thread against the same account
    assert await sr.start_trade_events(wake) is True
    await sr.stop_trade_events()


@pytest.mark.asyncio
async def test_monitor_loop_wakes_early_on_a_trade_event():
    """End to end through run_monitor_loop: with a long poll interval, a trade
    event must still produce a second scan quickly. This is the regression that
    would catch someone reverting the wait_for() back to a plain sleep()."""
    from vesper.monitor import run_monitor_loop

    wake_holder = {}

    async def fake_start(wake):
        wake_holder["wake"] = wake
        return True

    cycles = []

    async def fake_cycle(self, live=False):
        cycles.append(1)
        if len(cycles) == 1:
            # fire a "fill" as soon as the first cycle completes
            asyncio.get_running_loop().call_later(0.05, wake_holder["wake"].set)
        return []

    with patch("vesper.stream_runner.start_trade_events", side_effect=fake_start), \
         patch("vesper.monitor.PositionMonitor.run_monitoring_cycle", new=fake_cycle):
        task = asyncio.create_task(run_monitor_loop(interval_sec=30.0, live=False))
        await asyncio.sleep(1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(cycles) >= 2, (
        f"expected an early second scan from the trade event, got {len(cycles)} "
        "cycle(s) in 1s against a 30s interval — the loop is sleeping through fills"
    )
