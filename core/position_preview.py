"""Guard-free, read-only twin of `vesper.monitor.PositionMonitor` for
`trading_mcp`'s position-monitor-preview tool (feature M0-05).

**Why this module exists rather than just importing `vesper.monitor`.**
`vesper/monitor.py` module-scope does
`from vesper.execution_guard import guard, GuardError, TradingDisabled` --
that's the live `execution_guard.guard` singleton, constructed as an import
side effect. `PositionMonitor.evaluate_position()` and
`.poll_webull_positions()` never touch `guard` at runtime (only
`execute_exit_cascade()`, which this module deliberately does not
reimplement, does), but merely *importing* `vesper.monitor` -- even to reach
those two read-only methods -- already pulls `vesper.execution_guard` into
`sys.modules`. `trading_mcp/` is a read-only, owner-only viewer that must
never import `vesper.execution_guard` at all (see
`tests/test_trading_mcp.py`'s AST pin and CLAUDE.md rule 3), so this module
re-implements just the read side against `core.wb`, with zero import of
`vesper.execution_guard` or `vesper.bot` anywhere in this file.

`vesper/monitor.py`'s `PositionMonitor` is intentionally left untouched --
this is a parallel read-only copy for preview purposes, not a refactor of
the live exit cascade. `evaluate_position()` below is a byte-for-byte copy
of `PositionMonitor.evaluate_position()`'s rule logic (rules 1-8: peak-gain/
breakeven-lock tracking, take-profit, breakeven stop, stop-loss, the
underlying-keyed swing stop, the SPY gamma-flip crossing, the 0DTE time
stop, and the earnings-exit date). If those rules ever change in
`vesper/monitor.py`, update them here too -- that's the maintenance cost of
the guard-free import boundary, and it's a small, deterministic file to
keep in sync.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PreviewPosition:
    """Read-only mirror of `vesper.monitor.MonitoredPosition`'s fields."""

    symbol: str
    quantity: int
    entry_price: float
    current_price: float
    asset_type: str = "EQUITY"  # EQUITY or OPTION
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None  # CALL or PUT
    peak_gain_pct: float = 0.0
    breakeven_locked: bool = False
    contract_symbol: Optional[str] = None
    underlying_stop_type: Optional[str] = None
    underlying_stop_basis: Optional[str] = None
    earnings_exit_date: Optional[str] = None

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price

    @property
    def is_0dte(self) -> bool:
        if self.asset_type != "OPTION" or not self.expiry:
            return False
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.expiry == today_str


@dataclass
class PreviewExitTrigger:
    """Read-only mirror of `vesper.monitor.ExitTrigger`."""

    position: PreviewPosition
    reason: str
    urgency: str = "HIGH"
    sell_quantity: int = 0
    est_proceeds: float = 0.0
    pnl_pct: float = 0.0


class PositionPreviewMonitor:
    """Same deterministic `evaluate_position()` rules and the same
    `poll_webull_positions()` read as `vesper.monitor.PositionMonitor`, with
    no `execute_exit_cascade` (or anything else that could sell) and no
    import of `vesper.execution_guard` / `vesper.bot` anywhere in this
    class's module.
    """

    def __init__(
        self,
        take_profit_pct: float = 0.50,
        stop_loss_pct: float = -0.40,
        trailing_lock_pct: float = 0.25,
        time_stop_hour_et: int = 15,
        time_stop_minute_et: int = 0,
    ):
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.trailing_lock_pct = trailing_lock_pct
        self.time_stop_hour_et = time_stop_hour_et
        self.time_stop_minute_et = time_stop_minute_et

    def evaluate_position(
        self,
        pos: PreviewPosition,
        current_time_et: Optional[datetime] = None,
        spy_spot: Optional[float] = None,
        spy_gamma_flip: Optional[float] = None,
        underlying_technicals: Optional[Dict[str, Any]] = None,
    ) -> Optional[PreviewExitTrigger]:
        """Evaluate the same deterministic exit rules
        `vesper.monitor.PositionMonitor.evaluate_position()` does -- kept in
        sync with that method by hand; see this module's docstring."""
        pnl = pos.unrealized_pnl_pct

        # 1. Update peak gain & check trailing breakeven lock
        if pnl > pos.peak_gain_pct:
            pos.peak_gain_pct = pnl
        if pos.peak_gain_pct >= self.trailing_lock_pct and not pos.breakeven_locked:
            pos.breakeven_locked = True
            logger.info(f"🔒 Breakeven lock activated for {pos.symbol} (Peak: +{pos.peak_gain_pct:.1%})")

        # 2. Hard Take-Profit (+50%)
        if pnl >= self.take_profit_pct:
            return PreviewExitTrigger(
                position=pos,
                reason="TAKE_PROFIT",
                sell_quantity=pos.quantity,
                est_proceeds=pos.quantity * pos.current_price * (100 if pos.asset_type == "OPTION" else 1),
                pnl_pct=pnl,
            )

        # 3. Trailing Breakeven Stop (if locked and dropped back below entry)
        if pos.breakeven_locked and pnl <= 0.0:
            return PreviewExitTrigger(
                position=pos,
                reason="BREAKEVEN_STOP",
                sell_quantity=pos.quantity,
                est_proceeds=pos.quantity * pos.current_price * (100 if pos.asset_type == "OPTION" else 1),
                pnl_pct=pnl,
            )

        # 4. Hard Stop-Loss (-40%)
        if pnl <= self.stop_loss_pct:
            return PreviewExitTrigger(
                position=pos,
                reason="STOP_LOSS",
                urgency="CRITICAL",
                sell_quantity=pos.quantity,
                est_proceeds=pos.quantity * pos.current_price * (100 if pos.asset_type == "OPTION" else 1),
                pnl_pct=pnl,
            )

        # 5. Underlying-Keyed Swing-Option Stop
        if (
            pos.asset_type == "OPTION"
            and pos.underlying_stop_type == "underlying_level"
            and pos.underlying_stop_basis
            and underlying_technicals is not None
        ):
            level = underlying_technicals.get(pos.underlying_stop_basis)
            underlying_close = underlying_technicals.get("close")
            if level is not None and underlying_close is not None:
                is_put = (pos.option_type or "").upper() == "PUT"
                breached = underlying_close > level if is_put else underlying_close < level
                if breached:
                    return PreviewExitTrigger(
                        position=pos,
                        reason="UNDERLYING_LEVEL_STOP",
                        urgency="CRITICAL",
                        sell_quantity=pos.quantity,
                        est_proceeds=pos.quantity * pos.current_price * 100,
                        pnl_pct=pnl,
                    )

        # 6. Dealer Gamma Flip Crossing (SPY Call with spot below Gamma Flip)
        if pos.symbol.startswith("SPY") and pos.option_type == "CALL" and spy_spot and spy_gamma_flip:
            if spy_spot < spy_gamma_flip:
                return PreviewExitTrigger(
                    position=pos,
                    reason="GAMMA_FLIP_VIOLATION",
                    sell_quantity=pos.quantity,
                    est_proceeds=pos.quantity * pos.current_price * 100,
                    pnl_pct=pnl,
                )

        # 7. Time-Based Exit for 0DTE (>= 3:00 PM ET)
        now_et = current_time_et or datetime.now(timezone.utc)
        if pos.is_0dte and (now_et.hour > self.time_stop_hour_et or (now_et.hour == self.time_stop_hour_et and now_et.minute >= self.time_stop_minute_et)):
            return PreviewExitTrigger(
                position=pos,
                reason="TIME_STOP",
                sell_quantity=pos.quantity,
                est_proceeds=pos.quantity * pos.current_price * 100,
                pnl_pct=pnl,
            )

        # 8. Earnings-Week CSP Vega Harvest Exit
        if pos.earnings_exit_date:
            try:
                exit_date = datetime.strptime(pos.earnings_exit_date, "%Y-%m-%d").date()
                if now_et.date() >= exit_date:
                    return PreviewExitTrigger(
                        position=pos,
                        reason="EARNINGS_EXIT",
                        sell_quantity=pos.quantity,
                        est_proceeds=pos.quantity * pos.current_price * (100 if pos.asset_type == "OPTION" else 1),
                        pnl_pct=pnl,
                    )
            except ValueError:
                logger.warning(f"Malformed earnings_exit_date {pos.earnings_exit_date!r} for {pos.symbol}, skipping earnings-exit check")

        return None

    async def poll_webull_positions(self) -> List[PreviewPosition]:
        """Fetch open positions from Webull portfolio -- same read
        `vesper.monitor.PositionMonitor.poll_webull_positions()` does,
        against `core.wb.Webull` (already the guard-free module that one
        uses too)."""
        positions: List[PreviewPosition] = []
        try:
            from core.wb import Webull
            wb = Webull()
            if wb.configured:
                raw_portfolio = await asyncio.to_thread(wb.portfolio)
                raw_positions = raw_portfolio.get("positions", [])
                for p in raw_positions:
                    sym = p.get("symbol", "")
                    qty = int(p.get("quantity", 0))
                    cost = float(p.get("cost_price", 0.0) or p.get("last_price", 0.0))
                    last = float(p.get("last_price", cost))
                    is_opt = p.get("instrument_type") == "OPTION" or len(sym) > 6
                    positions.append(
                        PreviewPosition(
                            symbol=sym,
                            quantity=qty,
                            entry_price=cost,
                            current_price=last,
                            asset_type="OPTION" if is_opt else "EQUITY",
                            contract_symbol=sym if is_opt else None,
                        )
                    )
        except Exception as e:
            logger.warning(f"Could not poll Webull positions: {e}")
        return positions
