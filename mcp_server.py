"""MCP server — lets Claude drive sidecar by voice.

The intended setup is three windows side by side: Webull Desktop, the sidecar
deck, and Claude. Claude connects here over stdio and gets the whole broker
surface as tools, so "what am I holding?" and "buy two ONDS at eight forty" work
out loud instead of through a form.

**This is a thin bridge to a running sidecar, not a second broker client.** Every
tool is an HTTP call to `SIDECAR_URL` (default http://127.0.0.1:8787). That
matters more than it looks:

  - One authenticated Webull client, one 2FA token file. Two SDK clients
    desynchronise their token state in exactly the way you'd expect.
  - One rate-limit budget. Balance/positions/order-query share 2 req/2s across
    the *account*, not per process — a second client racing the deck is how you
    turn a working panel into a 429 loop.
  - One set of guards. The notional cap, the allowlist and the preview→confirm
    handshake live in `orders.py` and apply here for free. An MCP server that
    reimplemented them would drift from the deck's rules within a week.

Ordering is deliberately two-step, and that suits voice better than a form did:
`preview_order` returns a ticket plus a one-line summary and Webull's own cost
estimate, Claude reads it back, and only `place_order(ticket_id)` sends. The
ticket carries a hash of the exact payload, so what gets confirmed out loud is
byte-for-byte what reaches the broker. Set SIDECAR_ORDER_CONFIRM=0 on the
sidecar side if you'd rather skip that and have `place_order_now` work directly.

**Why stdio and not a remote connector.** Claude Desktop launches a stdio server
as a subprocess on your own machine, which is already on the tailnet, so it can
reach venus with no public hostname, no TLS, and no auth layer to get wrong. A
remote connector would mean exposing sidecar to the internet — and sidecar has
no authentication at all (rule 1), which matters far more now that it can trade.
`supermcp` is the repo that already solved OAuth; if this ever needs to be
reachable off the tailnet, it belongs there rather than bolted on here.

**MCP cannot be where alerts live.** A stdio server only runs while Claude
Desktop is talking to it, so an alert evaluated here would fire only during a
conversation. The watching is done by sidecar's own background thread
(`watcher.py`); the alert tools here just arm and inspect.

Run:  ./mcp.sh          (or: python mcp_server.py)

Claude Desktop config — either form works:

    { "mcpServers": { "sidecar": {
        "command": "/path/to/webull-sidecar/mcp.sh",
        "env": { "SIDECAR_URL": "http://100.113.21.73:8787" } } } }
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

SIDECAR_URL = os.environ.get("SIDECAR_URL", "http://127.0.0.1:8787").rstrip("/")
TIMEOUT = float(os.environ.get("SIDECAR_MCP_TIMEOUT", "30"))

INSTRUCTIONS = """Access to the user's Webull account through the sidecar app, \
plus TraderDaddy Pro dealer-gamma structure and price alerts.

This server CAN place, modify and cancel orders, and it spends real money.
Ordering is two-step and must stay that way: call preview_order (or
preview_option_order) first, read the returned `summary` and cost estimate back
to the user, and only call place_order with the ticket_id once they have
explicitly said yes. Never call place_order on your own initiative, and never to
test whether it works. Tickets are single-use and expire after 120 seconds.

Orders are also capped server-side. A rejection comes back as a plain sentence
naming the cap it broke — relay it rather than retrying with different numbers.

Dealer gamma is a map of where option hedging sits, NOT a forecast. Heavy gamma
marks where price often reacts on arrival; it never means price will travel
there, and a wall above spot is not a reason to be long. If a level read comes
back with flip_split set, the two models disagree about which side of the flip
price is on, so say the regime call is uncertain rather than picking one.

Alerts are evaluated by sidecar's own background thread, not here — this server
only arms and inspects them, so an alert keeps working after the conversation
ends."""

mcp = FastMCP("webull-sidecar", instructions=INSTRUCTIONS)


class SidecarDown(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(base_url=SIDECAR_URL, timeout=TIMEOUT)


def _call(method: str, path: str, **kw) -> Any:
    """One HTTP hop to sidecar, with errors phrased for a model to read aloud."""
    try:
        with _client() as c:
            r = c.request(method, path, **kw)
    except httpx.ConnectError as e:
        raise SidecarDown(
            f"sidecar is not reachable at {SIDECAR_URL}. Start it with ./run.sh "
            f"on the machine running Webull, or set SIDECAR_URL. ({e})"
        ) from e
    except httpx.TimeoutException as e:
        raise SidecarDown(f"sidecar timed out after {TIMEOUT}s: {e}") from e

    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            detail = body.get("detail") or json.dumps(body)[:400]
        except Exception:
            detail = r.text[:400]
        # 400 from the order path is a guard rejection — a real answer, not a
        # transport failure. Surface the reason verbatim so Claude can say it.
        raise RuntimeError(f"{detail}" if r.status_code == 400 else
                           f"sidecar returned HTTP {r.status_code}: {detail}")
    if not r.content:
        return {"ok": True}
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:2000]}


def _get(path: str, **params) -> Any:
    return _call("GET", path, params={k: v for k, v in params.items() if v is not None})


def _post(path: str, payload: dict | None = None) -> Any:
    return _call("POST", path, json=payload or {})


# ---------------------------------------------------------------------------
# Account & positions


@mcp.tool()
def get_portfolio() -> dict:
    """Current holdings, balances, and this app's risk guardrails.

    Returns every position (symbol, quantity, cost basis, live last price,
    market value, unrealized P/L), account totals including net liquidation and
    buying power, and the guardrail checks (concentration, correlated exposure,
    drawdown, dry powder, breadth) with their severity levels.

    Buying power is shared across accounts, so the total is a max, not a sum.
    Start here for any question about what the user owns or how they're doing.
    """
    return _get("/api/portfolio")


@mcp.tool()
def get_activities(account_id: str | None = None) -> dict:
    """Transaction history: fills, dividends, transfers, and fees."""
    return _get("/api/activities", account_id=account_id)


@mcp.tool()
def get_market_calendar(market_code: str = "US") -> Any:
    """Trading calendar — market open/closed days and session times."""
    return _get("/api/calendar", market_code=market_code)


# ---------------------------------------------------------------------------
# Market data


@mcp.tool()
def get_quote(symbols: str, category: str = "US_STOCK") -> dict:
    """Live quote for one or more symbols (comma-separated, e.g. "AAPL,MSFT").

    Returns last, bid/ask and sizes, open/high/low, volume, and change vs the
    previous close. Includes extended-hours prints, matching what Webull Desktop
    shows pre- and post-market.

    category: US_STOCK, US_ETF, US_OPTION, US_CRYPTO, US_FUTURES, US_EVENT.
    """
    return _get("/api/quote", symbols=symbols, category=category)


@mcp.tool()
def get_bars(symbol: str, timespan: str = "M5", count: int = 120,
             category: str = "US_STOCK", sessions: str | None = None) -> Any:
    """OHLCV price history for charting or trend questions.

    timespan: M1, M5, M15, M30, M60, D, W, M.
    sessions: RTH (regular hours), PRE, ATH (after hours). Default regular.
    """
    return _get("/api/bars", symbol=symbol, category=category,
                timespan=timespan, count=count, sessions=sessions)


@mcp.tool()
def get_order_book(symbol: str, levels: int = 5, category: str = "US_STOCK") -> Any:
    """Level 2 depth — resting bids and asks with sizes. Use to judge liquidity
    and spread before choosing a limit price."""
    return _get("/api/depth", symbol=symbol, category=category, levels=levels)


@mcp.tool()
def get_time_and_sales(symbol: str, count: int = 50, category: str = "US_STOCK") -> Any:
    """Recent individual trades (tick by tick) — who is hitting the bid or lifting the offer."""
    return _get("/api/tick", symbol=symbol, category=category, count=count)


@mcp.tool()
def get_order_flow(symbols: str, timespan: str = "M5", count: int = 30,
                   category: str = "US_STOCK") -> Any:
    """Footprint data: buy vs sell volume split at each price level."""
    return _get("/api/footprint", symbols=symbols, category=category,
                timespan=timespan, count=count)


@mcp.tool()
def get_auction_imbalance(symbol: str, action_type: str = "ALL") -> Any:
    """NASDAQ auction order imbalance (NOII). Only publishes during the opening
    and closing call auctions; empty outside those windows is normal."""
    return _get("/api/noii", symbol=symbol, action_type=action_type)


# ---------------------------------------------------------------------------
# Options


@mcp.tool()
def get_option_chain(underlying: str, expire_date: str | None = None,
                     option_type: str | None = None,
                     strike_gte: float | None = None,
                     strike_lte: float | None = None,
                     page_size: int = 200) -> Any:
    """Option contracts for an underlying, with their option symbols.

    expire_date: exact expiry as YYYY-MM-DD.
    option_type: CALL or PUT.
    strike_gte / strike_lte: bound the strikes so a chain stays readable.

    Use this to find the option symbol you need before quoting or trading it.
    """
    return _get("/api/options/chain", underlying=underlying, expire_date=expire_date,
                option_type=option_type, strike_gte=strike_gte, strike_lte=strike_lte,
                page_size=page_size)


@mcp.tool()
def get_option_quote(symbols: str) -> Any:
    """Live quotes for option contracts by option symbol (comma-separated)."""
    return _get("/api/options/quote", symbols=symbols)


# ---------------------------------------------------------------------------
# Research & screening


@mcp.tool()
def get_research(kind: str, symbol: str, statement: str = "indicators",
                 count: int = 4) -> Any:
    """Fundamental and analyst research on a symbol.

    kind:
      profile    company description, sector, market cap
      rating     analyst buy/hold/sell distribution
      target     analyst price targets
      flow       capital flow (institutional vs retail net buying)
      earnings   upcoming and past earnings dates
      dividend   dividend calendar
      filings    recent SEC filings
      eps        forward EPS estimates
      peers      industry comparison against peers
      financials financial statements — set `statement` to indicators,
                 income, cashflow, balance, or alert
    """
    return _get(f"/api/research/{kind}", symbol=symbol, statement=statement, count=count)


@mcp.tool()
def run_screener(kind: str, category: str = "US_STOCK", page_size: int = 20,
                 rank_type: str | None = None, sort_by: str | None = None) -> Any:
    """Market-wide screens.

    kind: gainers, losers, active (most active), sectors, dividend (high
    dividend), 52whl (52-week highs and lows).
    """
    return _get(f"/api/screener/{kind}", category=category, page_size=page_size,
                rank_type=rank_type, sort_by=sort_by)


@mcp.tool()
def get_signals() -> dict:
    """TraderDaddy Pro signals: market health, options flow put/call ratios, and
    community conviction scores for held names. Returns configured=false if no
    TD_API_KEY is set."""
    return _get("/api/signals")


@mcp.tool()
def get_gamma(symbol: str) -> Any:
    """Dealer-gamma structure for one symbol: spot, regime, gamma flip, max-gamma
    pin, key levels and the heaviest strikes near spot.

    A positioning map, NOT a forecast. Heavy gamma marks where price often
    reacts on arrival; it never means price will travel there. The flip is a
    regime boundary — above it dealer hedging dampens moves, below it amplifies
    them. If `flip_split` is true the two models disagree about which side price
    is on, so say the regime call is uncertain rather than picking one.

    A cold non-index name is computed upstream on first call (~2-4s).
    """
    return _get(f"/api/gex/{symbol.strip().upper()}")


# ---------------------------------------------------------------------------
# Alerts
#
# Arming and inspection only. The watching itself is sidecar's background
# thread: a stdio MCP server runs only while Claude Desktop is talking to it,
# so an alert evaluated here would fire only during a conversation.


@mcp.tool()
def list_alerts() -> Any:
    """Every price alert with its currently resolved level and state, plus
    watcher and delivery status."""
    return _get("/api/alerts")


@mcp.tool()
def create_alert(symbol: str, level: str | float, direction: str,
                 note: str = "", repeat: bool = False) -> Any:
    """Arm a price alert.

    `level` is either a number (e.g. 743.5) or a LIVE dealer level re-read on
    every check: 'flip', 'pin', 'wall_above', 'wall_below'.
    `direction` is the side price must end on: 'below' for a breakdown, 'above'
    for a breakout. Fires once unless repeat=True.

    An alert on a level price has ALREADY passed starts 'pending' and arms only
    once price returns to the other side, so it reports a genuine break rather
    than firing instantly.
    """
    # `level` must accept both arms: a model asked for "when SPY breaks 743"
    # sends the number, and for "when SPY loses the flip" sends the word.
    # Typing it as str alone rejects the numeric case outright.
    return _post("/api/alerts", {
        "symbol": symbol.strip().upper(), "level": level,
        "direction": direction.strip().lower(), "note": note, "repeat": repeat,
    })


@mcp.tool()
def delete_alert(alert_id: str) -> Any:
    """Delete an alert by its id (from list_alerts)."""
    return _call("DELETE", f"/api/alerts/{alert_id}")


@mcp.tool()
def test_alert_delivery() -> Any:
    """Send a test notification to prove the delivery path works. Use when the
    user doubts alerts would actually reach them."""
    return _post("/api/alerts/test")


# ---------------------------------------------------------------------------
# Watchlists


@mcp.tool()
def get_watchlists() -> Any:
    """All watchlists on the Webull account."""
    return _get("/api/watchlists")


@mcp.tool()
def get_watchlist_items(watchlist_id: str) -> Any:
    """Symbols in one watchlist."""
    return _get(f"/api/watchlists/{watchlist_id}")


@mcp.tool()
def create_watchlist(name: str) -> Any:
    """Create a watchlist. Syncs to the Webull app."""
    return _post("/api/watchlists", {"name": name})


@mcp.tool()
def add_to_watchlist(watchlist_id: str, instruments: list[dict]) -> Any:
    """Add instruments to a watchlist. Each entry needs an instrument_id —
    get one from get_option_chain or the instrument lookup."""
    return _post(f"/api/watchlists/{watchlist_id}/add", {"instruments": instruments})


@mcp.tool()
def remove_from_watchlist(watchlist_id: str, instruments: list[dict]) -> Any:
    """Remove instruments from a watchlist."""
    return _post(f"/api/watchlists/{watchlist_id}/remove", {"instruments": instruments})


# ---------------------------------------------------------------------------
# Orders — reads


@mcp.tool()
def get_open_orders() -> dict:
    """Working (unfilled) orders across all accounts.

    Includes orders placed in Webull Desktop, not just ones sent from here — the
    account is the source of truth, not this app.
    """
    return _get("/api/orders/open")


@mcp.tool()
def get_order_history(page_size: int = 50) -> dict:
    """Recent order history: filled, cancelled, and rejected orders."""
    return _get("/api/orders/history", page_size=page_size)


@mcp.tool()
def get_trading_config() -> dict:
    """The safety settings currently in force: whether trading is enabled,
    whether confirmation is required, the per-order notional cap, the quantity
    cap, and any symbol allowlist.

    Worth checking before explaining why an order was rejected.
    """
    return _get("/api/orders/config")


# ---------------------------------------------------------------------------
# Orders — writes
#
# Two steps by design. preview_order stages a ticket and returns Webull's own
# cost estimate; place_order sends it. Read the summary back to the user and get
# a spoken yes before calling place_order.


@mcp.tool()
def preview_order(
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    stop_price: float | None = None,
    time_in_force: str = "DAY",
    trading_session: str = "CORE",
    take_profit: float | None = None,
    stop_loss: float | None = None,
    algo_type: str | None = None,
    account_id: str | None = None,
) -> dict:
    """Price and stage an equity order WITHOUT sending it. Always call this first.

    Returns a ticket_id, a one-line human summary, Webull's cost estimate, and
    how long the ticket stays valid. Read the summary back to the user and wait
    for an explicit yes before calling place_order with the ticket_id.

    side: BUY, SELL, or SHORT.
    order_type: MARKET, LIMIT, STOP_LOSS, STOP_LOSS_LIMIT, TRAILING_STOP_LOSS.
      LIMIT needs limit_price; STOP_LOSS needs stop_price; STOP_LOSS_LIMIT needs both.
    time_in_force: DAY, GTC, or IOC.
    trading_session: CORE (regular hours), ALL (extended), NIGHT (overnight).
    take_profit / stop_loss: set either to turn this into a bracket order — the
      main order opens and these close it automatically.
    algo_type: TWAP, VWAP, or POV to work a larger order over time (US only).

    The order is rejected here, before reaching Webull, if it breaks the
    configured notional cap, quantity cap, or symbol allowlist. Those rejections
    come back as plain messages — relay them rather than retrying blindly.
    """
    spec: dict[str, Any] = {
        "symbol": symbol, "side": side.upper(), "quantity": quantity,
        "order_type": order_type.upper(), "time_in_force": time_in_force.upper(),
        "trading_session": trading_session.upper(), "kind": "equity",
    }
    if limit_price is not None:
        spec["limit_price"] = limit_price
    if stop_price is not None:
        spec["stop_price"] = stop_price
    if account_id:
        spec["account_id"] = account_id
    if take_profit is not None or stop_loss is not None:
        spec["bracket"] = {
            **({"take_profit": take_profit} if take_profit is not None else {}),
            **({"stop_loss": stop_loss} if stop_loss is not None else {}),
        }
    if algo_type:
        spec["algo_type"] = algo_type.upper()
        # The server requires a participation parameter per algo type; default it
        # rather than bounce the user for something they didn't ask about.
        if spec["algo_type"] == "POV":
            spec["target_vol_percent"] = 10
        else:
            spec["max_target_percent"] = 20
    return _post("/api/orders/preview", spec)


@mcp.tool()
def preview_option_order(
    legs: list[dict],
    quantity: float,
    side: str = "BUY",
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    option_strategy: str = "SINGLE",
    time_in_force: str = "DAY",
    account_id: str | None = None,
) -> dict:
    """Price and stage an option order WITHOUT sending it. Call before place_order.

    legs: one entry per leg, each with symbol (the underlying, e.g. "TSLA"),
    strike_price, option_expire_date (YYYY-MM-DD), option_type (CALL or PUT),
    side (BUY or SELL), and quantity. One leg for a single, two for a vertical,
    four for a condor.

    option_strategy: SINGLE, VERTICAL, STRADDLE, STRANGLE, CALENDAR, DIAGONAL,
    BUTTERFLY, CONDOR, IRON_CONDOR.

    Use get_option_chain first to confirm strikes and expiries exist.
    """
    spec: dict[str, Any] = {
        "legs": legs, "quantity": quantity, "side": side.upper(),
        "order_type": order_type.upper(), "option_strategy": option_strategy.upper(),
        "time_in_force": time_in_force.upper(), "kind": "option",
    }
    if limit_price is not None:
        spec["limit_price"] = limit_price
    if account_id:
        spec["account_id"] = account_id
    return _post("/api/orders/preview", spec)


@mcp.tool()
def place_order(ticket_id: str) -> dict:
    """SEND a previewed order to the broker. This spends real money.

    Only call this after preview_order (or preview_option_order) and after the
    user has explicitly confirmed the summary you read back to them. Do not call
    it on your own initiative, and never call it to "check" whether it works.

    Tickets are single-use and expire, so preview again rather than reusing one.
    """
    return _post("/api/orders/place", {"ticket_id": ticket_id})


@mcp.tool()
def discard_ticket(ticket_id: str) -> dict:
    """Throw away a staged ticket the user decided against."""
    return _call("DELETE", f"/api/orders/tickets/{ticket_id}")


@mcp.tool()
def replace_order(account_id: str, client_order_id: str,
                  quantity: float | None = None,
                  limit_price: float | None = None,
                  stop_price: float | None = None,
                  symbol: str | None = None,
                  kind: str = "equity") -> dict:
    """Amend a working order's quantity or price. Confirm with the user first —
    raising quantity or price increases exposure, and the same caps apply."""
    payload: dict[str, Any] = {"account_id": account_id,
                               "client_order_id": client_order_id, "kind": kind}
    for k, v in (("quantity", quantity), ("limit_price", limit_price),
                 ("stop_price", stop_price), ("symbol", symbol)):
        if v is not None:
            payload[k] = v
    return _post("/api/orders/replace", payload)


@mcp.tool()
def cancel_order(account_id: str, client_order_id: str, kind: str = "equity") -> dict:
    """Cancel one working order. Get the ids from get_open_orders."""
    return _post("/api/orders/cancel", {"account_id": account_id,
                                        "client_order_id": client_order_id, "kind": kind})


@mcp.tool()
def cancel_all_orders() -> dict:
    """Cancel every working order. Does not close positions — it only clears
    resting orders. Confirm with the user before calling."""
    return _post("/api/orders/cancel_all")


@mcp.tool()
def place_order_now(
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    stop_price: float | None = None,
    time_in_force: str = "DAY",
    trading_session: str = "CORE",
    account_id: str | None = None,
) -> dict:
    """Place an order in ONE step, skipping the preview/confirm handshake.

    This only works if the sidecar was started with SIDECAR_ORDER_CONFIRM=0;
    otherwise it returns a rejection telling you to preview first. Prefer
    preview_order + place_order — the two-step flow is what lets the user hear
    the order back before it goes.
    """
    spec: dict[str, Any] = {
        "symbol": symbol, "side": side.upper(), "quantity": quantity,
        "order_type": order_type.upper(), "time_in_force": time_in_force.upper(),
        "trading_session": trading_session.upper(), "kind": "equity",
    }
    if limit_price is not None:
        spec["limit_price"] = limit_price
    if stop_price is not None:
        spec["stop_price"] = stop_price
    if account_id:
        spec["account_id"] = account_id
    return _post("/api/orders/place_direct", spec)


# ---------------------------------------------------------------------------


@mcp.tool()
def get_health() -> dict:
    """Sidecar status: Webull connectivity, account count, TraderDaddy and chat
    credentials, whether trading is enabled, and the live-stream feeds.

    Check this first if a tool fails — it distinguishes "sidecar is down" from
    "Webull rejected us".
    """
    return _get("/api/health")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
