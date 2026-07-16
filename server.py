"""sidecar — a companion deck for Webull Desktop.

Run:  ./run.sh          (or: uvicorn server:app --port 8787)
Then: http://127.0.0.1:8787

Binds to loopback only. It holds live brokerage credentials and can place real
orders — do not expose it on a network interface.
"""

from __future__ import annotations

import os
from pathlib import Path

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import chat
import risk
from td import TDPro
from wb import Webull, WebullError

STATIC = Path(__file__).resolve().parent / "static"

# Hard ceiling on any single order placed through this UI. Deliberately low.
MAX_NOTIONAL = float(os.environ.get("SIDECAR_MAX_NOTIONAL", "25"))
# Placing an order requires echoing this string back — no accidental clicks.
CONFIRM_PHRASE = "PLACE"

app = FastAPI(title="sidecar", docs_url=None, redoc_url=None)

_wb: Webull | None = None
_td = TDPro()


def wb() -> Webull:
    global _wb
    if _wb is None:
        try:
            _wb = Webull()
        except WebullError as e:
            raise HTTPException(503, f"Webull unavailable: {e}") from e
    return _wb


class OrderReq(BaseModel):
    symbol: str = Field(min_length=1, max_length=12)
    side: str = Field(pattern="^(?i)(buy|sell)$")
    quantity: float = Field(gt=0)
    limit_price: float = Field(gt=0)
    confirm: str | None = None


@app.get("/api/portfolio")
def portfolio():
    p = wb().portfolio()
    p["risk"] = risk.evaluate(p)
    p["max_notional"] = MAX_NOTIONAL
    return p


@app.get("/api/signals")
def signals():
    try:
        symbols = [p["symbol"] for p in wb().portfolio()["positions"] if p["instrument_type"] == "EQUITY"]
    except HTTPException:
        symbols = []
    return _td.snapshot(symbols)


@app.get("/api/orders")
def orders():
    return {"open": wb().open_orders()}


@app.post("/api/preview")
def preview(req: OrderReq):
    client = wb()
    guard = risk.order_guard(req.symbol, req.side, req.quantity, req.limit_price,
                             client.portfolio(), MAX_NOTIONAL)
    try:
        res = client.preview(req.symbol, req.side, str(req.quantity), str(req.limit_price))
    except Exception as e:
        return {"ok": False, "error": str(e), "guard": guard}
    return {"ok": res["http"] == 200, "guard": guard, **res}


@app.post("/api/order")
def place(req: OrderReq):
    """Places a REAL order. Requires confirm == CONFIRM_PHRASE and a clean guard."""
    if req.confirm != CONFIRM_PHRASE:
        raise HTTPException(400, f"confirm must equal '{CONFIRM_PHRASE}' to place a live order")

    client = wb()
    guard = risk.order_guard(req.symbol, req.side, req.quantity, req.limit_price,
                             client.portfolio(), MAX_NOTIONAL)
    blocks = [g for g in guard if g["level"] == "block"]
    if blocks:
        return {"ok": False, "blocked": True, "guard": guard,
                "error": "; ".join(g["message"] for g in blocks)}

    try:
        res = client.place(req.symbol, req.side, str(req.quantity), str(req.limit_price))
    except Exception as e:
        return {"ok": False, "error": str(e), "guard": guard}
    return {"ok": res["http"] == 200, "guard": guard, **res}


@app.post("/api/cancel/{client_order_id}")
def cancel(client_order_id: str):
    try:
        return wb().cancel(client_order_id)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


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

    async def events():
        try:
            async for ev in chat.ask(req.prompt, session_id=req.session_id, model=req.model,
                                     portfolio=portfolio, signals=signals):
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
           "chat": label, "max_notional": MAX_NOTIONAL}
    try:
        out["webull"] = f"ok ({len(wb().accounts())} accounts)"
    except Exception as e:
        out["webull"] = f"error: {e}"
    return out


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
