"""Wiring for the restored alert stack (alerts + quotes + notify + watcher).

Those four modules predate the Vesper migration and were deleted wholesale in
`de60d51` along with `server.py`, which was the only thing that constructed
them. They came back unmodified (they are pure-stdlib and dependency-injected,
so they needed no adaptation), and this module is the small amount of glue
that `server.py` used to provide: build the store, point `Quotes` at the
current data sources, hand the watcher a `levels_of` that resolves dealer-gamma
levels through `td.levels()`, and start the thread.

Why the watcher is a THREAD and not an asyncio task is explained in
`watcher.py` itself and still applies here: the Webull SDK is synchronous and
blocking, and a slow snapshot inside the event loop would stall everything
sharing it -- which now means Vesper's graph and the Telegram poller rather
than the old SSE stream, but the reasoning is unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_watcher: Optional[Any] = None


def _build_levels_of():
    """Return a `levels_of(symbol) -> dict | None` backed by td.levels().

    Thin wrapper (M0-06) around `core.td.build_levels_of()`, which now owns
    the actual logic -- moved there so `trading_mcp/vesper_tools.py`'s
    `list_alerts` tool could import it without reaching into `vesper/` at
    all. Kept here, under this name, so `build_watcher()` below (and this
    module's own docstring/import surface for the live watcher thread) is
    unchanged.
    """
    from core.td import build_levels_of

    return build_levels_of()


def build_watcher(start: bool = True):
    """Construct (and by default start) the alert watcher. Idempotent —
    repeated calls return the already-running instance rather than starting
    a second thread against the same store."""
    global _watcher
    if _watcher is not None:
        return _watcher

    import alerts as alerts_mod
    import notify
    import core.quotes as quotes_mod
    import watcher as watcher_mod

    store = alerts_mod.AlertStore()

    # Quotes' fallback chain: Webull market-data snapshot -> portfolio
    # positions -> TDPro spot. Each source is optional; an unconfigured
    # Webull just means the chain starts one link further down.
    wb_data = None
    portfolio_fn = None
    try:
        from core.wb import Webull
        from core.md import Market

        wb = Webull()
        if wb.configured:
            wb_data = Market(wb)
            portfolio_fn = wb.portfolio
    except Exception as e:
        logger.warning(f"Alert watcher: Webull unavailable, quotes will fall back to TDPro: {e}")

    from core.td import TDPro

    q = quotes_mod.Quotes(
        wb_data=wb_data,
        portfolio_fn=portfolio_fn,
        td=TDPro(),
        on_log=lambda *a: logger.info("quotes: %s", " ".join(str(x) for x in a)),
    )

    _watcher = watcher_mod.Watcher(
        store=store,
        quotes=q,
        levels_of=_build_levels_of(),
        notifier=notify.Notifier(),
        on_log=lambda *a: logger.info("%s", " ".join(str(x) for x in a)),
    )
    if start:
        _watcher.start()
    return _watcher


def get_watcher():
    """Return the running watcher, or None if one was never built."""
    return _watcher
