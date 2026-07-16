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
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import chat
import risk
import safety
from td import TDPro
from wb import Webull, WebullError

STATIC = Path(__file__).resolve().parent / "static"

# Hard ceiling on any single order placed through this UI. Deliberately low.
MAX_NOTIONAL = float(os.environ.get("SIDECAR_MAX_NOTIONAL", "25"))

app = FastAPI(title="sidecar", docs_url=None, redoc_url=None)

_wb: Webull | None = None
_td = TDPro()
_arm = safety.ArmFlag()          # OFF on every boot, never persisted
_vault = safety.PreviewVault()   # single-use preview tokens, 60s TTL


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
    preview_token: str | None = None

    def bound(self) -> dict:
        """The fields a preview token is bound to."""
        return {"symbol": self.symbol.upper().strip(), "side": self.side.upper().strip(),
                "quantity": self.quantity, "limit_price": self.limit_price}


class ArmReq(BaseModel):
    armed: bool


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


@app.get("/api/arm")
def arm_state():
    """Arm state + what the Place confirmation expects."""
    try:
        last4 = safety.confirm_token(wb().equity_account_number())
    except Exception:
        last4 = ""
    return {**_arm.state(), "confirm_hint": f"last 4 of account (…{last4})" if last4 else "unavailable",
            "preview_ttl_secs": int(_vault.ttl())}


@app.post("/api/arm")
def set_arm(req: ArmReq):
    """Arm/disarm trading for this session. Never persisted — a restart is disarmed."""
    return _arm.set(req.armed)


def _guard_for(client: Webull, req: OrderReq) -> list[dict]:
    """Run the risk guard with the position context a SELL needs."""
    held = open_sells = None
    if req.side.upper() == "SELL":
        held = client.held_qty(req.symbol)
        open_sells = client.open_sell_qty(req.symbol)
    return risk.order_guard(req.symbol, req.side, req.quantity, req.limit_price,
                            client.portfolio(), MAX_NOTIONAL,
                            held_qty=held, open_sell_qty=open_sells or 0.0)


@app.post("/api/preview")
def preview(req: OrderReq):
    """Free, submits nothing. Mints a single-use token bound to these exact params
    and to one client_order_id, which place() must reuse."""
    client = wb()
    try:
        price = safety.check_tick(req.limit_price)
    except safety.BadPrice as e:
        return {"ok": False, "error": str(e), "guard": []}

    guard = _guard_for(client, req)
    coid = uuid.uuid4().hex          # minted ONCE, here — never on the place path
    try:
        res = client.preview(req.symbol, req.side, str(req.quantity),
                             safety.fmt_price(price), client_order_id=coid)
    except Exception as e:
        return {"ok": False, "error": str(e), "guard": guard}
    ok = res["http"] == 200
    token = (_vault.mint(req.bound(), coid)
             if ok and not any(g["level"] == "block" for g in guard) else None)
    return {"ok": ok, "guard": guard, "preview_token": token,
            "preview_ttl_secs": int(_vault.ttl()), **res}


@app.post("/api/order")
def place(req: OrderReq):
    """Places a REAL order. Four independent gates, in order:

      1. armed for this session (defaults OFF every boot)
      2. confirm == last 4 of the account number
      3. a live, single-use preview token whose params still match this order
      4. no blocking guard findings
    """
    if not _arm.armed:
        raise HTTPException(409, "trading is disarmed — arm the session first")

    client = wb()
    expected = safety.confirm_token(client.equity_account_number())
    if not expected or (req.confirm or "").upper() != expected:
        raise HTTPException(400, "confirm must be the last 4 of the account number")

    if not req.preview_token:
        raise HTTPException(400, "preview_token required — preview the order first")
    try:
        coid = _vault.consume(req.preview_token, req.bound())
    except (safety.PreviewExpired, safety.PreviewMismatch) as e:
        raise HTTPException(409, str(e)) from e

    try:
        price = safety.check_tick(req.limit_price)
    except safety.BadPrice as e:
        raise HTTPException(400, str(e)) from e

    guard = _guard_for(client, req)
    blocks = [g for g in guard if g["level"] == "block"]
    if blocks:
        return {"ok": False, "blocked": True, "guard": guard,
                "error": "; ".join(g["message"] for g in blocks)}

    try:
        # Reuse the preview's client_order_id: if this times out ambiguously and
        # the user retries, Webull rejects the duplicate rather than filling twice.
        res = client.place(req.symbol, req.side, str(req.quantity),
                           safety.fmt_price(price), client_order_id=coid)
    except Exception as e:
        safety.journal({"event": "place_failed", "client_order_id": coid, **req.bound(),
                        "error": str(e)})
        return {"ok": False, "error": str(e), "guard": guard,
                "warning": "place failed ambiguously — the order MAY have landed. "
                           "Check the Webull app before retrying."}

    # Journal before reporting success. If this raises, the order is already
    # live at Webull — say so rather than swallowing it.
    try:
        safety.journal({"event": "placed", "client_order_id": coid, **req.bound(), "http": res["http"], "result": res.get("result")})
    except Exception as e:
        return {"ok": True, "guard": guard, **res,
                "warning": f"order placed but journal write failed ({e}) — verify in the Webull app"}
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
