"""sidecar — a companion deck for Webull Desktop.

Run:  ./run.sh          (or: uvicorn server:app --port 8787)
Then: http://127.0.0.1:8787

Binds to loopback only by default. It holds live brokerage credentials AND — as
of the order path in `orders.py` — can trade with them, while having no
authentication of its own. Loopback or Tailscale, never 0.0.0.0. See run.sh.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import chat
import risk
from md import Market, category_for
from orders import Orders, OrderError
from stream import bus, streams
from td import TDPro
from wb import Webull, WebullError

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="sidecar", docs_url=None, redoc_url=None)

_wb: Webull | None = None
_md: Market | None = None
_orders: Orders | None = None
_td = TDPro()


def wb() -> Webull:
    global _wb
    if _wb is None:
        try:
            _wb = Webull()
        except WebullError as e:
            raise HTTPException(503, f"Webull unavailable: {e}") from e
    return _wb


def market() -> Market:
    """Market-data client. Imported by orders.py to price market orders."""
    global _md
    if _md is None:
        _md = Market(wb())
    return _md


def orders() -> Orders:
    global _orders
    if _orders is None:
        _orders = Orders(wb())
    return _orders


def _held_symbols(portfolio: dict) -> dict[str, str]:
    """symbol -> market-data category, for whatever the account currently holds."""
    return {p["symbol"]: category_for(p["instrument_type"])
            for p in portfolio.get("positions", []) if p.get("symbol")}


def _ensure_streams(portfolio: dict) -> None:
    """Start (or retarget) the push feeds against current holdings.

    Called opportunistically from the portfolio route rather than at import: it
    needs credentials and an account list, and a deck that can't reach Webull
    should still serve its UI shell.
    """
    try:
        streams.start(wb(), _held_symbols(portfolio))
    except Exception:
        pass  # REST polling still carries the deck; stream status shows the failure


# ---------------------------------------------------------------------------
# Portfolio & account


@app.get("/api/portfolio")
def portfolio(live: bool = True):
    p = wb().portfolio()
    p["risk"] = risk.evaluate(p)
    if live:
        _merge_quotes(p)
    _ensure_streams(p)
    return p


def _merge_quotes(p: dict) -> None:
    """Overlay live quotes onto positions.

    Position rows carry `last_price`, but it only refreshes on the account poll,
    which is capped at 2 req/2s. Snapshots are on the 600/min bucket, so this
    costs effectively nothing and makes the deck track the desktop.
    """
    positions = p.get("positions") or []
    equities = [x["symbol"] for x in positions
                if category_for(x["instrument_type"]) in ("US_STOCK", "US_ETF")]
    if not equities:
        return
    try:
        quotes = market().snapshot(equities)
    except Exception:
        return
    for pos in positions:
        q = quotes.get(pos["symbol"])
        if not q or q.get("error") or not q.get("last"):
            continue
        last = q["last"]
        pos["last_price"] = last
        pos["quote"] = q
        if pos["quantity"]:
            pos["market_value"] = last * pos["quantity"]
            pos["unrealized_pl"] = pos["market_value"] - pos["cost"]
            pos["unrealized_pl_pct"] = (pos["unrealized_pl"] / pos["cost"]) if pos["cost"] else 0.0
    t = p.get("totals") or {}
    t["market_value"] = sum(x["market_value"] for x in positions)
    t["unrealized_pl"] = sum(x["unrealized_pl"] for x in positions)
    t["unrealized_pl_pct"] = (t["unrealized_pl"] / t["cost"]) if t.get("cost") else 0.0
    t["winners"] = sum(1 for x in positions if x["unrealized_pl"] > 0)
    t["losers"] = sum(1 for x in positions if x["unrealized_pl"] < 0)
    p["risk"] = risk.evaluate(p)


@app.get("/api/activities")
def activities(account_id: str | None = None):
    return {"items": wb().activities(account_id)}


@app.get("/api/calendar")
def calendar(market_code: str = "US"):
    return wb().trade_calendar(market_code)


# ---------------------------------------------------------------------------
# Market data


@app.get("/api/quote")
def quote(symbols: str, category: str = "US_STOCK"):
    return market().snapshot([s.strip().upper() for s in symbols.split(",")], category)


@app.get("/api/bars")
def bars(symbol: str, category: str = "US_STOCK", timespan: str = "M5",
         count: int = 120, sessions: str | None = None):
    return market().bars(symbol.upper(), category, timespan, count, sessions)


@app.get("/api/depth")
def depth(symbol: str, category: str = "US_STOCK", levels: int = 5):
    return market().quotes(symbol.upper(), category, levels)


@app.get("/api/tick")
def tick(symbol: str, category: str = "US_STOCK", count: int = 50):
    return market().tick(symbol.upper(), category, count)


@app.get("/api/footprint")
def footprint(symbols: str, category: str = "US_STOCK", timespan: str = "M5", count: int = 30):
    return market().footprint([s.strip().upper() for s in symbols.split(",")], category, timespan, count)


@app.get("/api/noii")
def noii(symbol: str, action_type: str = "ALL"):
    return market().noii(symbol.upper(), action_type)


@app.get("/api/options/chain")
def option_chain(underlying: str, expire_date: str | None = None,
                 option_type: str | None = None, strike_gte: float | None = None,
                 strike_lte: float | None = None, page_size: int = 200):
    return market().option_chain(underlying.upper(), expire_date, option_type,
                                 strike_gte, strike_lte, page_size)


@app.get("/api/options/quote")
def option_quote(symbols: str):
    return market().option_snapshot([s.strip() for s in symbols.split(",")])


@app.get("/api/instrument")
def instrument(symbols: str, category: str = "US_STOCK"):
    return market().instrument(symbols.upper(), category)


@app.get("/api/research/{kind}")
def research(kind: str, symbol: str, statement: str = "indicators", count: int = 4):
    m = market()
    fns = {
        "profile": lambda: m.profile(symbol),
        "rating": lambda: m.analyst_rating(symbol),
        "target": lambda: m.analyst_target(symbol),
        "flow": lambda: m.capital_flow(symbol),
        "earnings": lambda: m.earnings_calendar(symbol),
        "dividend": lambda: m.dividend_calendar(symbol),
        "filings": lambda: m.sec_filings(symbol),
        "eps": lambda: m.forecast_eps(symbol),
        "peers": lambda: m.industry_comparison(symbol),
        "financials": lambda: m.financials(symbol, statement, count),
    }
    fn = fns.get(kind)
    if fn is None:
        raise HTTPException(404, f"unknown research kind: {kind} (try {sorted(fns)})")
    try:
        return fn()
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@app.get("/api/screener/{kind}")
def screener(kind: str, category: str = "US_STOCK", page_size: int = 20,
             rank_type: str | None = None, sort_by: str | None = None):
    try:
        return market().screener(kind, category, page_size, rank_type, sort_by)
    except Exception as e:
        raise HTTPException(502, str(e)) from e


# ---------------------------------------------------------------------------
# Watchlists


class WatchlistReq(BaseModel):
    name: str | None = None
    watchlist_id: str | None = None
    instruments: list[dict] | None = None


@app.get("/api/watchlists")
def watchlists():
    return market().watchlists()


@app.get("/api/watchlists/{watchlist_id}")
def watchlist_items(watchlist_id: str):
    return market().watchlist_items(watchlist_id)


@app.post("/api/watchlists")
def watchlist_create(req: WatchlistReq):
    if not req.name:
        raise HTTPException(400, "name is required")
    return market().watchlist_create(req.name)


@app.delete("/api/watchlists/{watchlist_id}")
def watchlist_delete(watchlist_id: str):
    return market().watchlist_delete(watchlist_id)


@app.post("/api/watchlists/{watchlist_id}/add")
def watchlist_add(watchlist_id: str, req: WatchlistReq):
    return market().watchlist_add(watchlist_id, req.instruments or [])


@app.post("/api/watchlists/{watchlist_id}/remove")
def watchlist_remove(watchlist_id: str, req: WatchlistReq):
    return market().watchlist_remove(watchlist_id, req.instruments or [])


# ---------------------------------------------------------------------------
# Orders
#
# Read routes first, then the write path. Everything that can move money goes
# through orders.py, which runs the caps and the preview/confirm handshake.


@app.get("/api/orders/config")
def orders_config():
    return Orders.config()


@app.get("/api/orders/open")
def orders_open():
    return {"orders": wb().open_orders()}


@app.get("/api/orders/history")
def orders_history(page_size: int = 50):
    return {"orders": wb().order_history(page_size)}


@app.get("/api/orders/tickets")
def orders_tickets():
    return {"tickets": orders().pending_tickets()}


class OrderSpec(BaseModel):
    account_id: str | None = None
    symbol: str | None = None
    side: str = "BUY"
    quantity: float | str | None = None
    order_type: str = "LIMIT"
    limit_price: float | str | None = None
    stop_price: float | str | None = None
    time_in_force: str = "DAY"
    entrust_type: str = "QTY"
    trading_session: str = "CORE"
    instrument_type: str = "EQUITY"
    market: str = "US"
    amount: float | str | None = None
    bracket: dict | None = None
    trailing_type: str | None = None
    trailing_stop_step: float | str | None = None
    algo_type: str | None = None
    algo_start_time: str | None = None
    algo_end_time: str | None = None
    max_target_percent: int | None = None
    target_vol_percent: int | None = None
    option_strategy: str | None = None
    legs: list[dict] | None = None
    kind: str = Field(default="equity", pattern="^(equity|option)$")


class TicketReq(BaseModel):
    ticket_id: str


class ReplaceReq(BaseModel):
    account_id: str
    client_order_id: str
    kind: str = "equity"
    symbol: str | None = None
    side: str | None = None
    order_type: str | None = None
    quantity: float | str | None = None
    limit_price: float | str | None = None
    stop_price: float | str | None = None
    algo_start_time: str | None = None
    algo_end_time: str | None = None
    max_target_percent: int | None = None
    target_vol_percent: int | None = None


class CancelReq(BaseModel):
    account_id: str
    client_order_id: str
    kind: str = "equity"


@app.post("/api/orders/preview")
def orders_preview(spec: OrderSpec):
    try:
        return orders().preview(spec.model_dump(exclude_none=True), spec.kind, origin="ui")
    except OrderError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/orders/place")
def orders_place(req: TicketReq):
    try:
        return orders().place_ticket(req.ticket_id)
    except OrderError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/orders/place_direct")
def orders_place_direct(spec: OrderSpec):
    """Only works with SIDECAR_ORDER_CONFIRM=0; otherwise 400s by design."""
    try:
        return orders().place_direct(spec.model_dump(exclude_none=True), spec.kind)
    except OrderError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@app.delete("/api/orders/tickets/{ticket_id}")
def orders_discard(ticket_id: str):
    return {"discarded": orders().discard(ticket_id)}


@app.post("/api/orders/replace")
def orders_replace(req: ReplaceReq):
    body = req.model_dump(exclude_none=True)
    try:
        return orders().replace(req.account_id, req.client_order_id, body, req.kind)
    except OrderError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/orders/cancel")
def orders_cancel(req: CancelReq):
    try:
        return orders().cancel(req.account_id, req.client_order_id, req.kind)
    except OrderError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/orders/cancel_all")
def orders_cancel_all():
    try:
        return orders().cancel_all()
    except OrderError as e:
        raise HTTPException(400, str(e)) from e


# ---------------------------------------------------------------------------
# Streaming


@app.get("/api/stream/status")
def stream_status():
    return streams.status()


@app.get("/api/stream")
async def stream():
    """SSE fan-out of MQTT quotes and gRPC trade events.

    Sends the last known value per topic on connect so a freshly opened tab
    renders immediately instead of waiting for every symbol to tick.
    """
    q = bus.subscribe()

    async def events():
        try:
            for ev in list(bus.latest.values()):
                yield f"data: {json.dumps(ev)}\n\n"
            yield f"data: {json.dumps({'type': 'stream', 'state': 'ready'})}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # keep proxies and the tab honest
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Signals & chat


@app.get("/api/signals")
def signals():
    try:
        symbols = [p["symbol"] for p in wb().portfolio()["positions"] if p["instrument_type"] == "EQUITY"]
    except HTTPException:
        symbols = []
    return _td.snapshot(symbols)


class ChatReq(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None
    model: str | None = None


@app.get("/api/chat/status")
def chat_status():
    kind, label = chat.credential()
    return {"configured": kind is not None, "kind": kind, "label": label,
            "model": chat.DEFAULT_MODEL}


@app.post("/api/chat")
async def chat_stream(req: ChatReq):
    """Stream a chat turn as SSE so text renders as it's generated.

    Live portfolio/signal state is injected into the turn rather than fetched by
    the model: we already hold it, and WebFetch can't read our loopback server
    (it upgrades http:// to https://).
    """
    portfolio = signals_data = None
    try:
        portfolio = wb().portfolio()
        portfolio["risk"] = risk.evaluate(portfolio)
    except Exception:
        pass  # chat still works without it; the model is told what it has
    try:
        syms = [p["symbol"] for p in (portfolio or {}).get("positions", []) if p["instrument_type"] == "EQUITY"]
        signals_data = _td.snapshot(syms)
    except Exception:
        pass

    open_orders: list[dict] = []
    try:
        open_orders = wb().open_orders()
    except Exception:
        pass

    async def events():
        try:
            async for ev in chat.ask(req.prompt, session_id=req.session_id, model=req.model,
                                     portfolio=portfolio, signals=signals_data,
                                     open_orders=open_orders):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    kind, label = chat.credential()
    cfg = Orders.config()
    out: dict[str, Any] = {
        "webull": "unknown",
        "tdpro": "configured" if _td.configured else "no TD_API_KEY",
        "chat": label,
        "trading": "enabled" if cfg["trading_enabled"] else "disabled",
        "confirm_required": cfg["require_confirm"],
        "streams": streams.status(),
    }
    try:
        out["webull"] = f"ok ({len(wb().accounts())} accounts)"
    except Exception as e:
        out["webull"] = f"error: {e}"
    return out


@app.on_event("shutdown")
def _shutdown():
    streams.stop()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
