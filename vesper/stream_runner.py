"""Wiring for the restored push feeds (`stream.py`).

`stream.py` came back from `de60d51^` unmodified, but its only consumer was
`server.py`'s SSE endpoint, which is gone. This module gives the gRPC
**trade-event** feed a new consumer that actually matters to Vesper: it wakes
the position monitor the moment an order/position event arrives, instead of
letting it wait out the next poll interval.

Why this is worth the wiring. `monitor.py` polls `wb.portfolio()` every 15s
and enforces a -40% stop on 0DTE positions. Between the migration deleting
`stream.py` and now, every fill -- including one you place by hand in Webull
Desktop -- took up to 15s to become visible, and every exit decision was made
against data up to 15s stale. On a 0DTE contract that is a lot of price.
`stream.py`'s own docstring made this the point of the feed: "an order you
place in Webull Desktop pushes a fill here within a second, so the deck stops
being a polled approximation of what the desktop already knows."

Degradation is deliberate and total: if MQTT/gRPC egress is blocked, the SDK
is missing, or credentials are unset, nothing here raises into the monitor --
the wake event simply never fires and the loop behaves exactly as it does
today, polling on its timer. Push is an accelerator, never a dependency.

The QUOTE feed (MQTT) is intentionally NOT started here. Nothing in Vesper
consumes per-tick quotes today -- `quotes.py` polls snapshots on the watcher's
own 15s tick, which is well inside the 600/min bucket -- so starting an MQTT
subscription would add a connection, a thread and a failure mode for no
current consumer. Revisit if something ever needs sub-second quotes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Event kinds that mean "the account's positions may have just changed."
# "option" is included: an option assignment/exercise changes the position set
# exactly as much as an equity fill does.
_POSITION_CHANGING = {"order", "position", "option"}

_events: Optional[object] = None
_consumer_task: Optional[asyncio.Task] = None


async def start_trade_events(wake: asyncio.Event) -> bool:
    """Start the gRPC trade-event feed and set `wake` on every event that could
    change the position set. Returns True if the feed actually started.

    Never raises -- a caller that gets False just keeps polling on its timer.
    """
    global _events, _consumer_task

    if _events is not None:
        return True

    try:
        import stream as stream_mod
        from core.wb import Webull, credentials

        wb = Webull()
        if not wb.configured:
            logger.info("Trade-event stream not started: Webull not configured")
            return False

        key, secret, region = credentials()
        account_ids = await asyncio.to_thread(wb.account_ids)
        if not account_ids:
            logger.info("Trade-event stream not started: no account ids")
            return False

        # Subscribe BEFORE starting the feed so no event is missed in the gap.
        queue = stream_mod.bus.subscribe()

        events = stream_mod.EventStream(key, secret, region, account_ids)
        events.start()
        _events = events

        async def _consume() -> None:
            while True:
                try:
                    evt = await queue.get()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug(f"trade-event consumer: {e}")
                    continue
                if not isinstance(evt, dict):
                    continue
                if evt.get("type") == "event" and evt.get("kind") in _POSITION_CHANGING:
                    logger.info(
                        f"⚡ Trade event ({evt.get('kind')}) — waking position monitor early"
                    )
                    wake.set()

        _consumer_task = asyncio.create_task(_consume(), name="trade-event-consumer")
        logger.info(f"Trade-event stream started for {len(account_ids)} account(s)")
        return True
    except Exception as e:
        # Missing SDK, blocked egress, bad credentials -- all land here, and all
        # mean the same thing to the caller: keep polling.
        logger.warning(f"Trade-event stream unavailable (monitor will poll only): {e}")
        return False


async def stop_trade_events() -> None:
    global _events, _consumer_task
    if _consumer_task is not None:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except (asyncio.CancelledError, Exception):
            pass
        _consumer_task = None
    if _events is not None:
        try:
            _events.stop()
        except Exception:
            pass
        _events = None


def status() -> dict:
    if _events is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "connected": getattr(_events, "connected", False),
        "error": getattr(_events, "error", None),
        "last_message_at": getattr(_events, "last_message_at", None),
    }
