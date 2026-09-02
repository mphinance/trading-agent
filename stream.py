"""Real-time push: MQTT market data and gRPC trade events.

Two independent feeds, one bus:

  QuoteStream  MQTT (paho) via the SDK's DataStreamingClient — quote, snapshot
               and tick payloads for whatever symbols we're subscribed to.
  EventStream  gRPC via TradeEventsClient — order / position / option status
               changes for the account. This is the one that matters most for a
               sidecar: an order you place in *Webull Desktop* pushes a fill here
               within a second, so the deck stops being a polled approximation of
               what the desktop already knows.

Threading note. Both SDK clients call back on their own threads: paho runs a
network loop thread, and `TradeEventsClient.do_subscribe()` blocks outright, so
it gets a daemon thread of its own. FastAPI's SSE handler lives on the asyncio
loop. `Bus.publish` is the crossing point and uses `call_soon_threadsafe` — do
not await anything from a callback thread, and do not let a slow SSE client
block a broker thread (queues drop instead of applying backpressure).

Both feeds are optional. If MQTT can't connect, the REST snapshot poll in md.py
keeps the deck correct — just less immediate — so failures here degrade rather
than break.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Any

log = logging.getLogger("sidecar.stream")

# A slow browser tab must never stall the MQTT loop, so subscriber queues are
# bounded and drop their oldest event when full.
QUEUE_MAX = 512
RECONNECT_BACKOFF = (2, 5, 10, 30, 60)


class Bus:
    """Fan-out from broker threads to any number of asyncio consumers."""

    def __init__(self) -> None:
        self._subs: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._lock = threading.Lock()
        # Last value per topic, so a tab opened mid-session renders immediately
        # instead of waiting for the next tick on every symbol.
        self.latest: dict[str, dict] = {}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subs.add((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = {(l, s) for (l, s) in self._subs if s is not q}

    def publish(self, event: dict) -> None:
        """Called from MQTT / gRPC threads. Never blocks, never raises."""
        key = event.get("key")
        if key:
            self.latest[key] = event
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._offer, q, event)
            except RuntimeError:
                # Loop already closed — the SSE handler's finally clause will
                # unsubscribe; nothing useful to do from this thread.
                pass

    @staticmethod
    def _offer(q: asyncio.Queue, event: dict) -> None:
        if q.full():
            try:
                q.get_nowait()  # drop oldest; a stale quote helps nobody
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


bus = Bus()


class QuoteStream:
    """MQTT market-data subscription, resubscribed as holdings change."""

    def __init__(self, app_key: str, app_secret: str, region_id: str) -> None:
        self._key, self._secret, self._region = app_key, app_secret, region_id
        self._client: Any = None
        self._symbols: dict[str, str] = {}  # symbol -> category
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False
        self.error: str | None = None
        self.last_message_at: float | None = None

    def start(self, symbols: dict[str, str]) -> None:
        with self._lock:
            self._symbols = dict(symbols)
            if self._thread and self._thread.is_alive():
                self._resubscribe()
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="wb-quotes", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.loop_stop()
            except Exception:
                pass

    def update_symbols(self, symbols: dict[str, str]) -> None:
        with self._lock:
            if symbols == self._symbols:
                return
            self._symbols = dict(symbols)
        self._resubscribe()

    def _resubscribe(self) -> None:
        client = self._client
        if client is None or not self.connected:
            return
        try:
            client.unsubscribe(unsubscribe_all=True)
            self._subscribe_all(client)
        except Exception as e:
            log.warning("resubscribe failed: %s", e)

    def _subscribe_all(self, client) -> None:
        from webull.data.common.subscribe_type import SubscribeType

        by_category: dict[str, list[str]] = {}
        with self._lock:
            for sym, cat in self._symbols.items():
                by_category.setdefault(cat, []).append(sym)
        sub_types = [SubscribeType.QUOTE.name, SubscribeType.SNAPSHOT.name, SubscribeType.TICK.name]
        for cat, syms in by_category.items():
            if syms:
                client.subscribe(syms, cat, sub_types)

    def _run(self) -> None:
        from webull.data.data_streaming_client import DataStreamingClient

        attempt = 0
        while not self._stop.is_set():
            try:
                session_id = uuid.uuid4().hex
                client = DataStreamingClient(self._key, self._secret, self._region, session_id)

                def on_connect(c, api_client, quotes_session_id):
                    self.connected = True
                    self.error = None
                    bus.publish({"type": "stream", "feed": "quotes", "state": "connected"})
                    self._subscribe_all(c)

                def on_message(c, topic, quotes):
                    self.last_message_at = time.time()
                    for ev in _quote_events(topic, quotes):
                        bus.publish(ev)

                client.on_connect_success = on_connect
                client.on_quotes_message = on_message
                self._client = client

                # Blocks until the connection drops; the SDK runs paho's network
                # loop underneath and dispatches callbacks from it.
                client.connect_and_loop_forever(logger_enable=False)
                attempt = 0
            except Exception as e:
                self.error = str(e)
                log.warning("quote stream error: %s", e)
            finally:
                self.connected = False
                bus.publish({"type": "stream", "feed": "quotes", "state": "disconnected",
                             "error": self.error})
            if self._stop.is_set():
                break
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            self._stop.wait(delay)


class EventStream:
    """gRPC order/position/option status push for the account.

    `do_subscribe` is synchronous and blocks for the life of the stream, so it
    owns a daemon thread and reconnects with backoff.
    """

    def __init__(self, app_key: str, app_secret: str, region_id: str,
                 account_ids: list[str]) -> None:
        self._key, self._secret, self._region = app_key, app_secret, region_id
        self._accounts = list(account_ids)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False
        self.error: str | None = None
        self.last_message_at: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wb-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from webull.trade.events.types import (
            EVENT_TYPE_ORDER,
            EVENT_TYPE_POSITION,
            EVENT_TYPE_OPTION,
        )
        from webull.trade.trade_events_client import TradeEventsClient

        attempt = 0
        while not self._stop.is_set():
            try:
                client = TradeEventsClient(self._key, self._secret, self._region)

                def on_connect(*_a, **_kw):
                    self.connected = True
                    self.error = None
                    bus.publish({"type": "stream", "feed": "events", "state": "connected"})

                def on_message(event_type, subscribe_type, payload, raw_message):
                    self.last_message_at = time.time()
                    kind = {
                        EVENT_TYPE_ORDER: "order",
                        EVENT_TYPE_POSITION: "position",
                        EVENT_TYPE_OPTION: "option",
                    }.get(event_type, str(event_type))
                    bus.publish({
                        "type": "event",
                        "kind": kind,
                        "subscribe_type": str(subscribe_type),
                        "payload": _scrub(payload),
                        "at": time.time(),
                    })

                client.on_connect = on_connect
                client.on_events_message = on_message
                client.on_log = lambda level, msg: log.debug("events: %s", msg)

                client.do_subscribe(self._accounts)  # blocks
                attempt = 0
            except Exception as e:
                self.error = str(e)
                log.warning("event stream error: %s", e)
            finally:
                self.connected = False
                bus.publish({"type": "stream", "feed": "events", "state": "disconnected",
                             "error": self.error})
            if self._stop.is_set():
                break
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            self._stop.wait(delay)


class Streams:
    """Both feeds plus their lifecycle, so server.py has one thing to hold."""

    def __init__(self) -> None:
        self.quotes: QuoteStream | None = None
        self.events: EventStream | None = None
        self._started = False

    def start(self, webull, symbols: dict[str, str]) -> None:
        if self._started:
            self.retarget(symbols)
            return
        from core.wb import credentials

        key, secret, region = credentials()
        self.quotes = QuoteStream(key, secret, region)
        self.quotes.start(symbols)
        try:
            self.events = EventStream(key, secret, region, webull.account_ids())
            self.events.start()
        except Exception as e:
            log.warning("event stream not started: %s", e)
        self._started = True

    def retarget(self, symbols: dict[str, str]) -> None:
        if self.quotes:
            self.quotes.update_symbols(symbols)

    def stop(self) -> None:
        for s in (self.quotes, self.events):
            if s:
                s.stop()
        self._started = False

    def status(self) -> dict:
        def one(s):
            if s is None:
                return {"enabled": False}
            return {
                "enabled": True,
                "connected": s.connected,
                "error": s.error,
                "last_message_at": s.last_message_at,
            }
        return {
            "quotes": one(self.quotes),
            "events": one(self.events),
            "subscribers": bus.subscriber_count,
        }


streams = Streams()


def _quote_events(topic: str, quotes: Any) -> list[dict]:
    """Turn one MQTT payload into UI events.

    The SDK hands back decoded payload objects whose shape varies by subscribe
    type, so read defensively: a missing field should cost one quote, not the
    whole feed.
    """
    rows = quotes if isinstance(quotes, list) else [quotes]
    out: list[dict] = []
    for row in rows:
        d = row if isinstance(row, dict) else getattr(row, "__dict__", None)
        if not isinstance(d, dict):
            continue
        symbol = d.get("symbol") or d.get("Symbol")
        if not symbol:
            continue
        last = _num(d.get("last_price") or d.get("close") or d.get("trade_price") or d.get("price"))
        out.append({
            "type": "quote",
            "key": f"quote:{symbol}",
            "symbol": symbol,
            "topic": topic,
            "last": last,
            "bid": _num(d.get("bid_price")),
            "ask": _num(d.get("ask_price")),
            "volume": _num(d.get("volume")),
            "change": _num(d.get("change")),
            "change_pct": _num(d.get("change_ratio")),
            "at": time.time(),
        })
    return out


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _scrub(payload: Any) -> Any:
    """Drop credential-shaped values before an event reaches the browser.

    This panel gets streamed on video and event payloads are broker-shaped, not
    UI-shaped — belt and braces alongside the scrub in index.html.
    """
    if isinstance(payload, dict):
        return {k: ("***" if _secretish(k) else _scrub(v)) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_scrub(v) for v in payload]
    if isinstance(payload, str) and (payload.startswith("sk-ant-") or payload.startswith("td_live_")):
        return "***"
    return payload


def _secretish(key: str) -> bool:
    k = key.lower()
    return any(t in k for t in ("secret", "token", "api_key", "apikey", "password", "signature"))
