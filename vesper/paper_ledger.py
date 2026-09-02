"""Paper Trading Simulated Fill Ledger & Mark-to-Market Accounting.

Tracks all simulated fills, open positions, daily mark-to-market valuations,
realized/unrealized PnL, and cash balances in `data/paper_ledger.json`.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_LEDGER_PATH = _DATA_DIR / "paper_ledger.json"
DEFAULT_STARTING_CASH = 100_000.0


def _load_ledger() -> Dict[str, Any]:
    if not _LEDGER_PATH.exists():
        return {
            "account": {
                "initial_cash": DEFAULT_STARTING_CASH,
                "cash": DEFAULT_STARTING_CASH,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
                "swept_premium": 0.0,
                "tax_reserve_swept": 0.0,
                "total_nlv": DEFAULT_STARTING_CASH,
                "last_marked_at": datetime.now(timezone.utc).isoformat(),
            },
            "fills": [],
            "closed_trades": [],
        }
    try:
        with open(_LEDGER_PATH) as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("Ledger data is not a dict")
            # Ensure swept_premium/tax_reserve_swept are initialized
            acc = data.setdefault("account", {})
            if "swept_premium" not in acc:
                acc["swept_premium"] = 0.0
            if "tax_reserve_swept" not in acc:
                acc["tax_reserve_swept"] = 0.0
            return data
    except Exception as e:
        logger.warning(f"Failed to load paper ledger: {e}")
        return {
            "account": {
                "initial_cash": DEFAULT_STARTING_CASH,
                "cash": DEFAULT_STARTING_CASH,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
                "swept_premium": 0.0,
                "tax_reserve_swept": 0.0,
                "total_nlv": DEFAULT_STARTING_CASH,
                "last_marked_at": datetime.now(timezone.utc).isoformat(),
            },
            "fills": [],
            "closed_trades": [],
        }


def _save_ledger(data: Dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _LEDGER_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, _LEDGER_PATH)


def _record_multileg_paper_fill(
    proposal: Any,
    result: Any,
    session_id: Optional[str],
    ledger: Dict[str, Any],
) -> Dict[str, Any]:
    """Record each leg of a multi-leg (combo) proposal as its own fill.

    The top-level proposal.limit_price/quantity/side describe only the
    "primary" leg for display purposes (see OrderLeg's docstring in
    state.py) -- using them here would book a synthetic long as if it were
    a single call purchase and silently drop the short put's credit and its
    own market-value-based P&L on close.
    """
    account = ledger.setdefault("account", {})
    fills = ledger.setdefault("fills", [])
    legs = getattr(proposal, "legs", [])
    ticker = getattr(proposal, "ticker", "")
    order_id = getattr(result, "order_proposal_id", getattr(proposal, "id", ""))
    strategy_type = getattr(proposal, "strategy_type", None)
    now = datetime.now(timezone.utc).isoformat()
    current_cash = float(account.get("cash", DEFAULT_STARTING_CASH))
    leg_summaries = []

    for i, leg in enumerate(legs):
        side = str(getattr(leg, "side", "BUY")).upper()
        leg_asset_type = str(getattr(leg, "asset_type", "OPTION")).upper()
        quantity = int(getattr(leg, "quantity", 1))
        filled_price = float(getattr(leg, "limit_price", 0.0))
        # Every combo leg was an option until Thega (100 shares + covered
        # call + CSPs) added a mixed equity+options combo -- an EQUITY leg's
        # "quantity" is shares, not contracts, so it needs multiplier 1.0,
        # not 100.0, or its cash impact would be booked 100x too large.
        multiplier = 1.0 if leg_asset_type == "EQUITY" else 100.0
        total_cost = round(filled_price * quantity * multiplier, 2)

        fill_id = f"fill-{ticker}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        fills.append({
            "id": fill_id,
            "order_proposal_id": order_id,
            "leg_index": i,
            "session_id": session_id or "N/A",
            "ticker": ticker,
            "asset_type": leg_asset_type,
            "side": side,
            "quantity": quantity,
            "filled_price": filled_price,
            "multiplier": multiplier,
            "total_cost": total_cost,
            "strike": getattr(leg, "strike", None),
            "option_type": getattr(leg, "option_type", None),
            "expiry": getattr(leg, "expiry", None),
            "strategy_type": strategy_type,
            # Strategy-level stop metadata belongs to the combo as a whole, not
            # to any one leg -- read off the top-level proposal (see playbooks.py
            # Branch 4 / Synthetic Long, the only combo that sets these today).
            "underlying_stop_type": getattr(proposal, "underlying_stop_type", None),
            "underlying_stop_basis": getattr(proposal, "underlying_stop_basis", None),
            "timestamp": now,
            "status": "OPEN",
            "current_price": filled_price,
            "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0,
        })

        if side in ("BUY", "LONG"):
            current_cash = round(current_cash - total_cost, 2)
        elif side in ("SELL", "SHORT"):
            current_cash = round(current_cash + total_cost, 2)
        leg_summaries.append(
            f"{side} {quantity}x {getattr(leg, 'option_type', '?')} ${getattr(leg, 'strike', '?')} @ ${filled_price:.2f}"
        )

    account["cash"] = current_cash
    _save_ledger(ledger)
    logger.info(f"📝 [PAPER FILL - MULTI-LEG] {strategy_type} {ticker}: " + " | ".join(leg_summaries))
    return {"status": "recorded", "legs": len(legs), "cash": current_cash}


def record_paper_fill(
    proposal: Any,
    result: Any,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a simulated fill from executor_node into paper ledger."""
    ledger = _load_ledger()
    account = ledger.setdefault("account", {})
    fills = ledger.setdefault("fills", [])

    if getattr(proposal, "legs", None):
        return _record_multileg_paper_fill(proposal, result, session_id, ledger)

    ticker = getattr(result, "ticker", getattr(proposal, "ticker", ""))
    order_id = getattr(result, "order_proposal_id", getattr(proposal, "id", ""))
    side = getattr(proposal, "side", "BUY").upper()
    asset_type = getattr(proposal, "asset_type", "EQUITY").upper()
    quantity = int(getattr(result, "filled_quantity", getattr(proposal, "quantity", 1)))
    filled_price = float(getattr(result, "filled_price", getattr(proposal, "limit_price", 0.0)))
    multiplier = 100.0 if asset_type == "OPTION" else 1.0
    total_cost = round(filled_price * quantity * multiplier, 2)

    now = datetime.now(timezone.utc).isoformat()
    fill_id = f"fill-{ticker}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

    fill_entry = {
        "id": fill_id,
        "order_proposal_id": order_id,
        "session_id": session_id or "N/A",
        "ticker": ticker,
        "asset_type": asset_type,
        "side": side,
        "quantity": quantity,
        "filled_price": filled_price,
        "multiplier": multiplier,
        "total_cost": total_cost,
        "stop_loss": getattr(proposal, "stop_loss", None),
        "profit_target": getattr(proposal, "profit_target", None),
        "strike": getattr(proposal, "strike", None),
        "option_type": getattr(proposal, "option_type", None),
        "strategy_type": getattr(proposal, "strategy_type", None),
        "underlying_stop_type": getattr(proposal, "underlying_stop_type", None),
        "underlying_stop_basis": getattr(proposal, "underlying_stop_basis", None),
        "earnings_exit_date": getattr(proposal, "earnings_exit_date", None),
        "timestamp": now,
        "status": "OPEN",
        "current_price": filled_price,
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
    }

    # BUY/LONG debits cash (paying for shares). SELL/SHORT (opening a short)
    # credits cash (proceeds from selling borrowed shares) — close_paper_position's
    # PnL math for a short assumes this credit happened here; without it, closing
    # a short double-counts the entry proceeds into account["cash"].
    current_cash = float(account.get("cash", DEFAULT_STARTING_CASH))
    if side in ("BUY", "LONG"):
        account["cash"] = round(current_cash - total_cost, 2)
    elif side in ("SELL", "SHORT"):
        account["cash"] = round(current_cash + total_cost, 2)

    fills.append(fill_entry)

    # If this fill is from a premium recycling proposal, mark the premium as swept.
    # If it's from the tax-reserve sweep, mark that independent pool instead --
    # the two id prefixes are mutually exclusive by construction, so a fill can
    # only ever match one of these branches.
    if str(order_id).startswith("prop-recycle-"):
        current_swept = float(account.get("swept_premium", 0.0))
        account["swept_premium"] = round(current_swept + total_cost, 2)
        logger.info(
            f"♻️ [PREMIUM RECYCLED] Swept ${total_cost:,.2f} into {quantity}x {ticker} "
            f"(Total swept: ${account['swept_premium']:,.2f})"
        )
    elif str(order_id).startswith("prop-taxsweep-"):
        current_tax_swept = float(account.get("tax_reserve_swept", 0.0))
        account["tax_reserve_swept"] = round(current_tax_swept + total_cost, 2)
        logger.info(
            f"🏦 [TAX RESERVE SWEPT] Swept ${total_cost:,.2f} into {quantity}x {ticker} "
            f"(Total tax-reserve swept: ${account['tax_reserve_swept']:,.2f})"
        )

    _save_ledger(ledger)

    logger.info(
        f"📝 [PAPER FILL] {side} {quantity}x {ticker} ({asset_type}) @ ${filled_price:.2f} (Cost: ${total_cost:,.2f})"
    )
    return fill_entry


def get_paper_positions() -> List[Dict[str, Any]]:
    """Return all currently open paper positions."""
    ledger = _load_ledger()
    return [f for f in ledger.get("fills", []) if f.get("status") == "OPEN"]


def close_paper_position(
    fill_id: str,
    close_price: float,
    reason: str = "EXIT",
) -> Optional[Dict[str, Any]]:
    """Close an open paper position and compute realized PnL."""
    ledger = _load_ledger()
    account = ledger.setdefault("account", {})
    fills = ledger.setdefault("fills", [])
    closed_trades = ledger.setdefault("closed_trades", [])

    for fill in fills:
        if fill.get("id") == fill_id and fill.get("status") == "OPEN":
            fill["status"] = "CLOSED"
            fill["closed_at"] = datetime.now(timezone.utc).isoformat()
            fill["close_price"] = round(close_price, 2)
            fill["close_reason"] = reason

            qty = fill.get("quantity", 1)
            entry_px = fill.get("filled_price", 0.0)
            multiplier = fill.get("multiplier", 1.0)
            side = fill.get("side", "BUY")

            if side == "BUY":
                pnl = (close_price - entry_px) * qty * multiplier
                proceeds = close_price * qty * multiplier
            else:
                pnl = (entry_px - close_price) * qty * multiplier
                proceeds = entry_px * qty * multiplier + pnl

            cost = fill.get("total_cost", 1.0) or 1.0
            pnl_pct = (pnl / cost) * 100.0

            fill["realized_pnl"] = round(pnl, 2)
            fill["realized_pnl_pct"] = round(pnl_pct, 2)

            # Update account
            current_cash = float(account.get("cash", DEFAULT_STARTING_CASH))
            current_realized = float(account.get("realized_pnl", 0.0))
            account["cash"] = round(current_cash + proceeds, 2)
            account["realized_pnl"] = round(current_realized + pnl, 2)

            closed_trades.append(fill)
            _save_ledger(ledger)
            logger.info(
                f"💰 [PAPER EXIT] Closed {fill['ticker']} @ ${close_price:.2f} (PnL: ${pnl:+,.2f} / {pnl_pct:+.1f}%)"
            )
            return fill
    return None


async def mark_to_market(live_quotes: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Recalculate open position valuations and mark-to-market account NLV."""
    ledger = _load_ledger()
    account = ledger.setdefault("account", {})
    fills = ledger.setdefault("fills", [])

    total_market_value = 0.0
    total_unrealized = 0.0

    for fill in fills:
        if fill.get("status") != "OPEN":
            continue

        ticker = fill.get("ticker", "")
        qty = fill.get("quantity", 1)
        entry_px = fill.get("filled_price", 0.0)
        multiplier = fill.get("multiplier", 1.0)
        side = fill.get("side", "BUY")

        # Resolve price
        current_px = None
        if live_quotes and ticker in live_quotes:
            current_px = live_quotes[ticker]
        else:
            try:
                from core.data import get_live_price
                current_px = await get_live_price(ticker)
            except Exception:
                current_px = entry_px

        if current_px is None or current_px <= 0:
            current_px = entry_px

        if side == "BUY":
            pos_unrealized = (current_px - entry_px) * qty * multiplier
            pos_value = current_px * qty * multiplier
        else:
            pos_unrealized = (entry_px - current_px) * qty * multiplier
            pos_value = (entry_px * qty * multiplier) + pos_unrealized

        cost = fill.get("total_cost", 1.0) or 1.0
        pos_pnl_pct = (pos_unrealized / cost) * 100.0

        fill["current_price"] = round(current_px, 2)
        fill["unrealized_pnl"] = round(pos_unrealized, 2)
        fill["unrealized_pnl_pct"] = round(pos_pnl_pct, 2)

        total_market_value += pos_value
        total_unrealized += pos_unrealized

    cash = float(account.get("cash", DEFAULT_STARTING_CASH))
    total_nlv = cash + total_market_value

    account["unrealized_pnl"] = round(total_unrealized, 2)
    account["total_nlv"] = round(total_nlv, 2)
    account["last_marked_at"] = datetime.now(timezone.utc).isoformat()

    _save_ledger(ledger)

    return {
        "cash": round(cash, 2),
        "market_value": round(total_market_value, 2),
        "unrealized_pnl": round(total_unrealized, 2),
        "realized_pnl": float(account.get("realized_pnl", 0.0)),
        "total_nlv": round(total_nlv, 2),
        "open_positions_count": sum(1 for f in fills if f.get("status") == "OPEN"),
        "last_marked_at": account["last_marked_at"],
    }


def get_paper_summary() -> Dict[str, Any]:
    """Return high-level summary of paper trading performance."""
    ledger = _load_ledger()
    account = ledger.get("account", {})
    fills = ledger.get("fills", [])
    closed = ledger.get("closed_trades", [])

    open_fills = [f for f in fills if f.get("status") == "OPEN"]
    init_cash = float(account.get("initial_cash", DEFAULT_STARTING_CASH))
    total_nlv = float(account.get("total_nlv", init_cash))
    total_return_pct = ((total_nlv - init_cash) / init_cash) * 100.0 if init_cash > 0 else 0.0

    wins = sum(1 for c in closed if (c.get("realized_pnl") or 0) > 0)
    win_rate = (wins / len(closed) * 100.0) if closed else 0.0

    realized_pnl = float(account.get("realized_pnl", 0.0))
    swept_premium = float(account.get("swept_premium", 0.0))
    # 75% of cumulative realized P&L is the free-share pool's ceiling. This was
    # 100% before the tax-reserve sweep below carved out the other 25% as an
    # independent pool -- see the "TAX RESERVE SWEEP" section in
    # vesper/nodes/playbooks.py for why these are two self-contained pools
    # rather than one shared number split at sweep time.
    unswept_premium = max(0.0, round(0.75 * realized_pnl - swept_premium, 2))
    tax_reserve_swept = float(account.get("tax_reserve_swept", 0.0))
    unswept_tax_reserve = max(0.0, round(0.25 * realized_pnl - tax_reserve_swept, 2))

    return {
        "initial_cash": init_cash,
        "cash": float(account.get("cash", init_cash)),
        "total_nlv": total_nlv,
        "total_return_pct": round(total_return_pct, 2),
        "realized_pnl": realized_pnl,
        "swept_premium": swept_premium,
        "unswept_premium": unswept_premium,
        "tax_reserve_swept": tax_reserve_swept,
        "unswept_tax_reserve": unswept_tax_reserve,
        "unrealized_pnl": float(account.get("unrealized_pnl", 0.0)),
        "open_positions_count": len(open_fills),
        "closed_trades_count": len(closed),
        "win_rate_pct": round(win_rate, 1),
        "last_marked_at": account.get("last_marked_at"),
    }


def mark_premium_swept(amount: float) -> float:
    """Mark an amount of realized options premium as swept into share accumulation."""
    ledger = _load_ledger()
    account = ledger.setdefault("account", {})
    current_swept = float(account.get("swept_premium", 0.0))
    new_swept = round(current_swept + amount, 2)
    account["swept_premium"] = new_swept
    _save_ledger(ledger)
    return new_swept


def get_unswept_premium() -> float:
    """Return available unswept realized options premium."""
    summary = get_paper_summary()
    return float(summary.get("unswept_premium", 0.0))


def mark_tax_reserve_swept(amount: float) -> float:
    """Mark an amount of realized P&L as swept into the tax reserve (25% pool)."""
    ledger = _load_ledger()
    account = ledger.setdefault("account", {})
    current_swept = float(account.get("tax_reserve_swept", 0.0))
    new_swept = round(current_swept + amount, 2)
    account["tax_reserve_swept"] = new_swept
    _save_ledger(ledger)
    return new_swept


def get_unswept_tax_reserve() -> float:
    """Return available unswept 25% tax-reserve pool."""
    summary = get_paper_summary()
    return float(summary.get("unswept_tax_reserve", 0.0))
