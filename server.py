"""sidecar — a companion deck for Webull Desktop.

Run:  ./run.sh          (or: uvicorn server:app --port 8787)
Then: http://127.0.0.1:8787

Binds to loopback only. It holds live brokerage credentials — read-only, but
still not something to expose on a network interface.
"""

from __future__ import annotations

from pathlib import Path

import json
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import alerts as alerts_mod
import chat
import risk
from quotes import Quotes
from td import TDPro
from watcher import Watcher
from wb import Webull, WebullError

STATIC = Path(__file__).resolve().parent / "static"

# Bound what reaches the upstream tool. Its own schema is ^[A-Za-z]{1,6}$, and
# a path segment is user input even on loopback.
_SYMBOL_RE = re.compile(r"^[A-Za-z]{1,6}$")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Attach the market-data client here rather than at import: it needs the
    # same credentials the trade client uses, and a credentials failure must not
    # stop the app from serving the UI. Quotes degrades to portfolio + TDPro
    # spot without it.
    try:
        _quotes.set_data_client(wb().data_client())
    except Exception as e:
        print(f"quotes: no Webull data client ({e}); using portfolio + TDPro spot")
    _watcher.start()
    yield
    _watcher.stop()


app = FastAPI(title="sidecar", docs_url=None, redoc_url=None, lifespan=lifespan)

_wb: Webull | None = None
_td = TDPro()
_store = alerts_mod.AlertStore()


def wb() -> Webull:
    global _wb
    if _wb is None:
        try:
            _wb = Webull()
        except WebullError as e:
            raise HTTPException(503, f"Webull unavailable: {e}") from e
    return _wb


def _portfolio_or_none() -> dict | None:
    try:
        return wb().portfolio()
    except Exception:
        return None


_quotes = Quotes(portfolio_fn=_portfolio_or_none, td=_td, on_log=print)
_watcher = Watcher(_store, _quotes, levels_of=_td.levels, on_log=print)




@app.get("/api/portfolio")
def portfolio():
    p = wb().portfolio()
    p["risk"] = risk.evaluate(p)
    return p


@app.get("/api/signals")
def signals():
    try:
        symbols = [p["symbol"] for p in wb().portfolio()["positions"] if p["instrument_type"] == "EQUITY"]
    except HTTPException:
        symbols = []
    return _td.snapshot(symbols)


@app.get("/api/gex/{symbol}")
def gex(symbol: str):
    """Dealer-gamma structure for one symbol.

    On-demand rather than polled: a cold non-index name is computed upstream on
    first call (~2-4s), so fanning this out across the book on a timer would be
    both slow and rude to the rate limit. td.levels() caches for 5 minutes.
    """
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(400, "symbol must be 1-6 letters")
    return _td.levels(symbol)


class AlertReq(BaseModel):
    symbol: str = Field(max_length=6)
    # Either a price, or one of alerts.DYNAMIC_LEVELS ("flip", "pin",
    # "wall_above", "wall_below") for a level re-read from TDPro every tick.
    level: str | float
    direction: str
    note: str = ""
    repeat: bool = False


@app.get("/api/alerts")
def list_alerts():
    out = []
    for a in _store.list():
        # Resolve for display only. A dynamic alert whose level cannot be read
        # right now shows null rather than a remembered number — the whole point
        # is that a stale level is worse than none.
        lv = _td.levels(a["symbol"]) if a["level_ref"] else None
        level = alerts_mod.resolve_level(a, lv)
        out.append({**a, "level_now": level, "describe": alerts_mod.describe(a, level)})
    return {"alerts": out, "watcher": _watcher.status()}


@app.post("/api/alerts")
def create_alert(req: AlertReq):
    try:
        a = alerts_mod.make_alert(req.symbol, req.level, req.direction,
                                  note=req.note, repeat=req.repeat)
    except alerts_mod.AlertError as e:
        raise HTTPException(400, str(e)) from e
    _store.add(a)
    # Seed the arming side immediately so a fresh alert is not blind until the
    # next watcher tick, and so "you're already past that level" is visible now.
    price = _quotes.get(a["symbol"], max_age=30.0)
    lv = _td.levels(a["symbol"]) if a["level_ref"] else None
    level = alerts_mod.resolve_level(a, lv)
    alerts_mod.evaluate(a, price, level)
    return {**a, "level_now": level, "describe": alerts_mod.describe(a, level),
            "price_now": price}


@app.delete("/api/alerts/{alert_id}")
def delete_alert(alert_id: str):
    if not _store.remove(alert_id):
        raise HTTPException(404, "no such alert")
    return {"ok": True}


@app.post("/api/alerts/test")
def test_alert():
    """Prove the delivery path end to end. Nothing else verifies Telegram."""
    ok = _watcher.telegram.send("sidecar test alert — delivery is working.")
    return {"sent": ok, "telegram": _watcher.telegram.status()}


class ChatReq(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None
    model: str | None = None
    # The symbol whose gamma structure the UI is showing. Its levels ride along
    # with the turn so "what's the gamma here" resolves without the user having
    # to say a ticker out loud — speech recognition mangles tickers badly.
    symbol: str | None = Field(default=None, max_length=6)


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
    portfolio = signals = None
    try:
        portfolio = wb().portfolio()
        portfolio["risk"] = risk.evaluate(portfolio)
    except Exception:
        pass  # chat still works without it; the model is told what it has
    try:
        syms = [p["symbol"] for p in (portfolio or {}).get("positions", []) if p["instrument_type"] == "EQUITY"]
        signals = _td.snapshot(syms)
    except Exception:
        pass

    levels = None
    if req.symbol and _SYMBOL_RE.match(req.symbol):
        try:
            levels = _td.levels(req.symbol)
        except Exception:
            pass  # a dead gamma read must not cost the user their turn

    async def events():
        try:
            async for ev in chat.ask(req.prompt, session_id=req.session_id, model=req.model,
                                     portfolio=portfolio, signals=signals, levels=levels):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/health")
def health():
    kind, label = chat.credential()
    out = {"webull": "unknown", "tdpro": "configured" if _td.configured else "no TD_API_KEY",
           "chat": label}
    try:
        out["webull"] = f"ok ({len(wb().accounts())} accounts)"
    except Exception as e:
        out["webull"] = f"error: {e}"
    return out


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
