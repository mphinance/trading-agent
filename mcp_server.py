"""MCP server exposing sidecar to Claude Desktop.

    Claude Desktop ──stdio──► mcp_server.py ──HTTP──► sidecar on venus
    (your machine)            (your machine)          (100.113.21.73:8787)

A **thin client**, deliberately. It holds no credentials, talks to no broker,
and reimplements no logic — every tool here is one HTTP call to a sidecar route
that already existed. sidecar stays the only process with the Webull keys.

Why stdio and not a remote connector: Claude Desktop launches a stdio server as
a subprocess on your own machine, which is already on the tailnet, so it can
reach venus with no public hostname, no TLS, and no auth layer to get wrong.
A remote connector would mean exposing sidecar to the internet — and sidecar has
no authentication at all (rule 1), so that is not a small change. supermcp is
the repo that already solved OAuth; if this ever needs to be reachable off the
tailnet, it belongs there rather than bolted on here.

**MCP cannot be where alerts live.** A stdio server only runs while Claude
Desktop is talking to it, so an alert evaluated here would fire only during a
conversation. The watching is done by sidecar's own background thread; these
tools just arm and inspect.

Read-only in the same sense as the rest of sidecar: it reads the account and
manages alerts. There is no order path here and there must not be one.

Install (Claude Desktop > Settings > Developer > Edit Config):

    {
      "mcpServers": {
        "sidecar": {
          "command": "/path/to/webull-sidecar/.venv/bin/python",
          "args": ["/path/to/webull-sidecar/mcp_server.py"],
          "env": { "SIDECAR_URL": "http://100.113.21.73:8787" }
        }
      }
    }

Needs `pip install mcp`. Restart Claude Desktop after editing the config.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from mcp.server import MCPServer

BASE = os.environ.get("SIDECAR_URL", "http://127.0.0.1:8787").rstrip("/")
TIMEOUT = float(os.environ.get("SIDECAR_TIMEOUT", "30"))

mcp = MCPServer(
    name="sidecar",
    instructions=(
        "Read-only access to Michael's Webull account via the sidecar app, plus "
        "TraderDaddy Pro dealer-gamma structure and price alerts.\n\n"
        "This server CANNOT place, modify or cancel orders. If asked to trade, say "
        "so and describe the trade instead.\n\n"
        "Dealer gamma is a map of where option hedging sits, NOT a forecast. Heavy "
        "gamma marks where price often reacts on arrival; it never means price will "
        "travel there, and a wall above spot is not a reason to be long. The gamma "
        "flip is a regime boundary: above it dealer hedging dampens moves, below it "
        "it amplifies them.\n\n"
        "Alerts can use a LIVE level rather than a frozen number: pass level='flip', "
        "'pin', 'wall_above' or 'wall_below' and it is re-read from TDPro on every "
        "check, so it tracks structure as it moves. Prefer those over a hardcoded "
        "price when the user describes a level by what it IS ('when it loses the "
        "flip') rather than by its number."
    ),
)


def _call(method: str, path: str, **kw) -> Any:
    try:
        r = requests.request(method, f"{BASE}{path}", timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        # Name the URL. The usual cause is sidecar not running or this machine
        # being off the tailnet, and both are invisible from the error alone.
        return {"error": f"cannot reach sidecar at {BASE} ({e}). Is it running, "
                         "and is this machine on the tailnet?"}
    if r.status_code >= 400:
        detail = ""
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = r.text[:200]
        return {"error": f"sidecar returned {r.status_code}: {detail}"}
    try:
        return r.json()
    except ValueError:
        return {"error": "sidecar returned a non-JSON response"}


@mcp.tool(description="Merged portfolio: positions, balances, totals and the app's "
                      "risk guardrails. Read-only.")
def get_portfolio() -> Any:
    return _call("GET", "/api/portfolio")


@mcp.tool(description="Dealer-gamma structure for one symbol: spot, regime, gamma flip, "
                      "max-gamma pin, key levels and the heaviest strikes near spot. "
                      "A positioning map, not a forecast.")
def get_gamma(symbol: str) -> Any:
    return _call("GET", f"/api/gex/{symbol.strip().upper()}")


@mcp.tool(description="TraderDaddy Pro market signals: market health, options-flow "
                      "put/call ratios, and per-symbol conviction for held names.")
def get_signals() -> Any:
    return _call("GET", "/api/signals")


@mcp.tool(description="List every price alert with its current resolved level and "
                      "state, plus watcher and delivery status.")
def list_alerts() -> Any:
    return _call("GET", "/api/alerts")


@mcp.tool(description=(
    "Arm a price alert. `level` is either a number (e.g. 743.5) or a LIVE dealer "
    "level re-read on every check: 'flip', 'pin', 'wall_above', 'wall_below'. "
    "`direction` is the side price must end on: 'below' for a breakdown, 'above' "
    "for a breakout. Fires once unless repeat=True. Notifies over Telegram.\n\n"
    "An alert on a level price has ALREADY passed starts 'pending' and arms only "
    "once price returns to the other side, so it reports a genuine break rather "
    "than firing instantly."
))
def create_alert(symbol: str, level: str | float, direction: str,
                 note: str = "", repeat: bool = False) -> Any:
    # `level` must accept both arms: a model asked for "when SPY breaks 743"
    # sends the number, and for "when SPY loses the flip" sends the word.
    # Typing it as str alone rejects the numeric case outright.
    return _call("POST", "/api/alerts", json={
        "symbol": symbol.strip().upper(), "level": level,
        "direction": direction.strip().lower(), "note": note, "repeat": repeat,
    })


@mcp.tool(description="Delete an alert by its id (from list_alerts).")
def delete_alert(alert_id: str) -> Any:
    return _call("DELETE", f"/api/alerts/{alert_id}")


@mcp.tool(description="Send a test notification to prove the Telegram delivery path "
                      "works. Use when the user doubts alerts would actually reach them.")
def test_alert_delivery() -> Any:
    return _call("POST", "/api/alerts/test")


if __name__ == "__main__":
    mcp.run(transport="stdio")
