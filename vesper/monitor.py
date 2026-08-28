"""Active Position Monitor & 0DTE Exit Cascade Loop (Module 3).

Monitors open Webull & paper positions in real-time, enforcing deterministic
take-profit (+50%), stop-loss (-40%), trailing breakeven (+25%), 3:00 PM time-stops,
and dynamic Dealer Gamma flip crossings.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from vesper.execution_guard import guard, GuardError, TradingDisabled
from vesper.bot.manager import channel_manager
from vesper.state import OrderProposal, ExecutionResult

logger = logging.getLogger(__name__)


@dataclass
class MonitoredPosition:
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
class ExitTrigger:
    position: MonitoredPosition
    reason: str  # TAKE_PROFIT, STOP_LOSS, BREAKEVEN_STOP, TIME_STOP, GAMMA_FLIP_VIOLATION
    urgency: str = "HIGH"
    sell_quantity: int = 0
    est_proceeds: float = 0.0
    pnl_pct: float = 0.0


class PositionMonitor:
    """Continuous position evaluator and exit cascade enforcer."""

    def __init__(
        self,
        take_profit_pct: float = 0.50,       # +50%
        stop_loss_pct: float = -0.40,        # -40%
        trailing_lock_pct: float = 0.25,     # +25% triggers breakeven stop
        time_stop_hour_et: int = 15,         # 3:00 PM ET
        time_stop_minute_et: int = 0,
    ):
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.trailing_lock_pct = trailing_lock_pct
        self.time_stop_hour_et = time_stop_hour_et
        self.time_stop_minute_et = time_stop_minute_et
        self.tracked_positions: Dict[str, MonitoredPosition] = {}

    def evaluate_position(
        self,
        pos: MonitoredPosition,
        current_time_et: Optional[datetime] = None,
        spy_spot: Optional[float] = None,
        spy_gamma_flip: Optional[float] = None,
    ) -> Optional[ExitTrigger]:
        """Evaluate deterministic exit rules for a single position."""
        pnl = pos.unrealized_pnl_pct

        # 1. Update peak gain & check trailing breakeven lock
        if pnl > pos.peak_gain_pct:
            pos.peak_gain_pct = pnl
        if pos.peak_gain_pct >= self.trailing_lock_pct and not pos.breakeven_locked:
            pos.breakeven_locked = True
            logger.info(f"🔒 Breakeven lock activated for {pos.symbol} (Peak: +{pos.peak_gain_pct:.1%})")

        # 2. Hard Take-Profit (+50%)
        if pnl >= self.take_profit_pct:
            return ExitTrigger(
                position=pos,
                reason="TAKE_PROFIT",
                sell_quantity=pos.quantity,
                est_proceeds=pos.quantity * pos.current_price * (100 if pos.asset_type == "OPTION" else 1),
                pnl_pct=pnl,
            )

        # 3. Trailing Breakeven Stop (if locked and dropped back below entry)
        if pos.breakeven_locked and pnl <= 0.0:
            return ExitTrigger(
                position=pos,
                reason="BREAKEVEN_STOP",
                sell_quantity=pos.quantity,
                est_proceeds=pos.quantity * pos.current_price * (100 if pos.asset_type == "OPTION" else 1),
                pnl_pct=pnl,
            )

        # 4. Hard Stop-Loss (-40%)
        if pnl <= self.stop_loss_pct:
            return ExitTrigger(
                position=pos,
                reason="STOP_LOSS",
                urgency="CRITICAL",
                sell_quantity=pos.quantity,
                est_proceeds=pos.quantity * pos.current_price * (100 if pos.asset_type == "OPTION" else 1),
                pnl_pct=pnl,
            )

        # 5. Dealer Gamma Flip Crossing (SPY Call with spot below Gamma Flip)
        if pos.symbol.startswith("SPY") and pos.option_type == "CALL" and spy_spot and spy_gamma_flip:
            if spy_spot < spy_gamma_flip:
                return ExitTrigger(
                    position=pos,
                    reason="GAMMA_FLIP_VIOLATION",
                    sell_quantity=pos.quantity,
                    est_proceeds=pos.quantity * pos.current_price * 100,
                    pnl_pct=pnl,
                )

        # 6. Time-Based Exit for 0DTE (>= 3:00 PM ET)
        now_et = current_time_et or datetime.now(timezone.utc)
        if pos.is_0dte and (now_et.hour > self.time_stop_hour_et or (now_et.hour == self.time_stop_hour_et and now_et.minute >= self.time_stop_minute_et)):
            return ExitTrigger(
                position=pos,
                reason="TIME_STOP",
                sell_quantity=pos.quantity,
                est_proceeds=pos.quantity * pos.current_price * 100,
                pnl_pct=pnl,
            )

        return None

    async def poll_webull_positions(self) -> List[MonitoredPosition]:
        """Fetch open positions from Webull portfolio."""
        positions = []
        try:
            from wb import Webull
            wb = Webull()
            if wb.configured:
                raw_portfolio = await asyncio.to_thread(wb.portfolio)
                raw_positions = raw_portfolio.get("positions", [])
                for p in raw_positions:
                    sym = p.get("symbol", "")
                    qty = int(p.get("quantity", 0))
                    cost = float(p.get("cost_price", 0.0) or p.get("last_price", 0.0))
                    last = float(p.get("last_price", cost))
                    # wb.py's position dict uses "instrument_type", not "asset_type" —
                    # the latter key never exists, so this used to silently fall back
                    # to the length heuristic alone every time.
                    is_opt = p.get("instrument_type") == "OPTION" or len(sym) > 6
                    positions.append(
                        MonitoredPosition(
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

    def poll_paper_positions(self) -> List[MonitoredPosition]:
        """Fetch open positions from Paper Ledger for dry-run monitoring."""
        positions = []
        try:
            from vesper.paper_ledger import get_paper_positions
            open_fills = get_paper_positions()
            for f in open_fills:
                sym = f.get("ticker", "")
                qty = int(f.get("quantity", 1))
                entry_px = float(f.get("filled_price", 0.0))
                cur_px = float(f.get("current_price", entry_px))
                asset_type = f.get("asset_type", "EQUITY")
                positions.append(
                    MonitoredPosition(
                        symbol=sym,
                        quantity=qty,
                        entry_price=entry_px,
                        current_price=cur_px,
                        asset_type=asset_type,
                        strike=f.get("strike"),
                        option_type=f.get("option_type"),
                    )
                )
        except Exception as e:
            logger.warning(f"Could not poll paper positions: {e}")
        return positions

    async def execute_exit_cascade(self, trigger: ExitTrigger, live: bool = False) -> ExecutionResult:
        """Executes the exit cascade order through ExecutionGuard."""
        pos = trigger.position
        logger.warning(f"🚨 EXIT TRIGGERED for {pos.symbol}: {trigger.reason} (PnL: {trigger.pnl_pct:+.1%})")

        # Alert external channels immediately
        await channel_manager.broadcast_alert(
            title=f"🚨 EXIT CASCADE: {pos.symbol} ({trigger.reason})",
            message=f"Triggered {trigger.reason} at PnL {trigger.pnl_pct:+.1%}. Liquidating {trigger.sell_quantity}x @ ${pos.current_price:.2f}.",
            level="WARNING",
        )

        proposal = OrderProposal(
            id=f"exit-{pos.symbol.lower()}-{int(datetime.now(timezone.utc).timestamp())}",
            ticker=pos.symbol,
            asset_type=pos.asset_type,
            side="SELL",
            order_type="MARKET",
            quantity=trigger.sell_quantity,
            limit_price=pos.current_price,
            estimated_cost=trigger.est_proceeds,
            max_risk=0.0,
            approved=True,
        )

        if not live:
            # Simulated Dry-Run Fill
            res = ExecutionResult(
                order_proposal_id=proposal.id,
                ticker=pos.symbol,
                status="DRY_RUN_SIMULATED",
                client_order_id=f"sim-exit-{proposal.id}",
                filled_quantity=proposal.quantity,
                filled_price=pos.current_price,
                message=f"Simulated {trigger.reason} exit fill for {pos.symbol}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            # Close open position in Paper Ledger (Item 2)
            try:
                from vesper.paper_ledger import get_paper_positions, close_paper_position
                for op in get_paper_positions():
                    if op.get("ticker") == pos.symbol and op.get("status") == "OPEN":
                        close_paper_position(op["id"], close_price=pos.current_price, reason=trigger.reason)
                        break
            except Exception as e:
                logger.warning(f"Could not close paper position for {pos.symbol}: {e}")

            await channel_manager.broadcast_execution(res)
            return res

        # Live Execution Guard Handshake — SELL is a risk-reducing side, but it
        # still goes through the same guard as every other order: a SELL with
        # a fat-fingered quantity is exactly as capable of doing damage as a
        # BUY, and the guard has no notion of "this one's safe."
        payload = {
            "symbol": pos.symbol,
            "side": "SELL",
            "quantity": proposal.quantity,
            "limit_price": pos.current_price,
            "order_type": "MARKET",
            "asset_type": pos.asset_type,
        }

        try:
            from wb import Webull
            wb = Webull()

            def _fetch_bp_and_account():
                account_id = wb.accounts()[0]["account_id"]
                try:
                    bp = wb.portfolio()["totals"]["buying_power"]
                except Exception:
                    bp = None
                return account_id, bp

            account_id, buying_power = await asyncio.to_thread(_fetch_bp_and_account)

            ticket = guard.preview(proposal.id, payload, live_buying_power=buying_power)

            place_res = await asyncio.to_thread(
                guard.place,
                ticket.id,
                payload,
                lambda: wb.trade.order_v2.place_order(
                    account_id=account_id,
                    stock_order_sub_request={
                        "symbol": pos.symbol,
                        "action": "SELL",
                        "order_type": "MARKET",
                        "quantity": proposal.quantity,
                    },
                ),
            )
            res = ExecutionResult(
                order_proposal_id=proposal.id,
                ticker=pos.symbol,
                status="SUBMITTED",
                client_order_id=ticket.id,
                message=f"Live {trigger.reason} exit order placed: {place_res.get('data', place_res) if isinstance(place_res, dict) else place_res}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            await channel_manager.broadcast_execution(res)
            return res
        except (GuardError, TradingDisabled) as e:
            res = ExecutionResult(
                order_proposal_id=proposal.id,
                ticker=pos.symbol,
                status="BLOCKED_BY_GUARDRAIL",
                message=f"Exit blocked by guardrail: {e}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            await channel_manager.broadcast_execution(res)
            return res
        except Exception as e:
            logger.error(f"Live exit cascade execution error: {e}")
            res = ExecutionResult(
                order_proposal_id=proposal.id,
                ticker=pos.symbol,
                status="FAILED",
                message=str(e),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            await channel_manager.broadcast_execution(res)
            return res

    async def run_monitoring_cycle(self, live: bool = False) -> List[ExecutionResult]:
        """Runs a single evaluation sweep across all positions."""
        positions = await self.poll_webull_positions()
        if not live:
            positions.extend(self.poll_paper_positions())
        results = []

        # Get current SPY Gamma Flip
        spy_spot = None
        spy_flip = None
        try:
            from td import TDPro
            td = TDPro()
            if td.configured:
                spy_levels = await asyncio.to_thread(td.levels, "SPY")
                spy_spot = float(spy_levels.get("spot", 0.0)) or None
                spy_flip = float(spy_levels.get("gamma_flip", 0.0)) or None
        except Exception:
            pass

        for pos in positions:
            # Update cache
            if pos.symbol not in self.tracked_positions:
                self.tracked_positions[pos.symbol] = pos
            else:
                existing = self.tracked_positions[pos.symbol]
                existing.current_price = pos.current_price
                pos = existing

            trigger = self.evaluate_position(pos, spy_spot=spy_spot, spy_gamma_flip=spy_flip)
            if trigger:
                res = await self.execute_exit_cascade(trigger, live=live)
                results.append(res)
                # Only drop tracking state (peak gain, breakeven lock) once the
                # exit actually happened — a BLOCKED/FAILED result means the
                # position is still open, and re-adding it fresh next cycle
                # would silently reset an already-armed breakeven stop.
                if res.status in ("SUBMITTED", "DRY_RUN_SIMULATED") and pos.symbol in self.tracked_positions:
                    del self.tracked_positions[pos.symbol]

        return results


async def run_monitor_loop(interval_sec: float = 15.0, live: bool = False, once: bool = False):
    """Continuous background loop for position monitoring."""
    monitor = PositionMonitor()
    print("\n" + "=" * 76)
    print(f"🛡️ VESPER ACTIVE POSITION MONITOR & EXIT CASCADE (Mode: {'LIVE' if live else 'DRY_RUN'})")
    print(f"Rules: Take Profit=+50% | Stop Loss=-40% | Trailing Breakeven=+25% | Time Stop=15:00 ET")
    print("=" * 76)

    while True:
        try:
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            print(f"[{now_str}] 🔍 Scanning open positions...")
            results = await monitor.run_monitoring_cycle(live=live)
            if results:
                for r in results:
                    print(f"   ➔ [{r.status}] {r.ticker}: {r.message}")
            else:
                print("   • All positions healthy within risk tolerances.")
        except Exception as e:
            logger.error(f"Error in monitor loop: {e}")

        if once:
            break
        await asyncio.sleep(interval_sec)
