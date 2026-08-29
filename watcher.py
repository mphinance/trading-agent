"""The background loop that makes alerts fire when nobody is looking.

This is deliberately NOT in the MCP layer. An MCP server only runs while Claude
is asking it something, so an alert defined there would only ever "fire" during
a conversation — which is precisely when you do not need one. sidecar is already
a boot-enabled systemd service, so the watching belongs here and MCP is only the
control surface.

The loop is a daemon thread rather than an asyncio task: the Webull SDK is
synchronous and blocking, and a slow snapshot call inside the event loop would
stall the SSE chat stream that shares it.
"""

from __future__ import annotations

import threading
import time

import alerts as alerts_mod
import notify
from vesper.metrics import metrics

# Fast enough that a break is caught within a candle on any timeframe worth
# alerting on, slow enough to stay far from Webull's limits. The snapshot call
# is batched across every watched symbol, so this is one request per tick
# regardless of how many alerts exist.
POLL_SEC = 15.0

# After a failure, back off rather than hammer. Caps so a long outage doesn't
# leave the watcher effectively asleep once the source recovers.
BACKOFF_START = 5.0
BACKOFF_MAX = 120.0


class Watcher:
    def __init__(self, store, quotes, levels_of, notifier=None, on_log=None) -> None:
        self.store = store
        self.quotes = quotes
        self.levels_of = levels_of
        self.notifier = notifier or notify.Notifier()
        self._log = on_log or (lambda *a: None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_tick: float | None = None
        self._last_error: str | None = None
        self._ticks = 0
        self._fired: list[dict] = []   # recent fires, for the UI
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="alert-watcher", daemon=True)
        self._thread.start()
        self._log("watcher: started")

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        with self._lock:
            recent = list(self._fired[-10:])
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "ticks": self._ticks,
            "last_tick": self._last_tick,
            "last_error": self._last_error,
            "watching": self.store.symbols(),
            "notify": self.notifier.status(),
            "quotes": self.quotes.status(),
            "recent": recent,
        }

    def _run(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            try:
                self._tick()
                self._last_error = None
                backoff = BACKOFF_START
                self._stop.wait(POLL_SEC)
            except Exception as e:
                # A watcher that dies on one bad tick is worse than no watcher,
                # because the UI still shows the alerts as armed.
                self._last_error = f"{type(e).__name__}: {e}"
                self._log(f"watcher: {self._last_error}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    def _tick(self) -> None:
        self._ticks += 1
        self._last_tick = time.time()
        symbols = self.store.symbols()
        if not symbols:
            return

        self.quotes.refresh(symbols)
        qstatus = self.quotes.status()
        metrics.record_quote_snapshot(qstatus.get("sources") or {}, qstatus.get("max_age_sec"))
        fired = self.store.sweep(
            price_of=lambda s: self.quotes.get(s, max_age=POLL_SEC * 2),
            levels_of=self.levels_of,
        )
        for rec in fired:
            self._deliver(rec)

    def _deliver(self, rec: dict) -> None:
        sym = rec["symbol"]
        source, age = self.quotes.source_of(sym), self.quotes.age_of(sym)
        rec["source"] = source
        text = notify.format_alert(rec, source, age)
        rec["delivered"] = self.notifier.send(text, notify.alert_title(rec))
        # Log either way. An alert that fired but could not be delivered is the
        # one case where silence would be actively misleading.
        self._log(f"ALERT {'sent' if rec['delivered'] else 'UNDELIVERED'}: {text.splitlines()[0]}")
        with self._lock:
            self._fired.append(rec)
            del self._fired[:-50]
