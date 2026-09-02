"""Active Position Monitor & 0DTE Exit Cascade Loop (Module 3).

Monitors open Webull & paper positions in real-time, enforcing deterministic
take-profit (+50%), stop-loss (-40%), trailing breakeven (+25%), 3:00 PM time-stops,
and dynamic Dealer Gamma flip crossings.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from datetime import datetime, timezone, time
from time import monotonic as _monotonic
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from vesper.execution_guard import guard, GuardError, TradingDisabled
from vesper.bot.manager import channel_manager
from vesper.metrics import metrics
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
    # Underlying-keyed swing-option stop (see evaluate_position step 5).
    # None on both means "no swing stop drafted for this position" -- it
    # keeps only the flat contract-pct stop above. Populated from the paper
    # ledger fill for paper positions; always None for Webull-sourced
    # positions today (see poll_webull_positions for why).
    underlying_stop_type: Optional[str] = None
    underlying_stop_basis: Optional[str] = None
    # Earnings-week CSP vega harvest exit tag (see OrderProposal.earnings_exit_date
    # and evaluate_position's EARNINGS_EXIT step). ISO date string or None.
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
        # Cycle-timing for status() -- kept local to this instance rather than
        # in vesper/metrics.py, same separation-of-concerns watcher.py's own
        # status() (ticks/last_tick/last_error, local to Watcher) models: this
        # is monitor-cycle-specific state, not a cross-cutting broker/LLM/
        # order signal.
        self._cycles = 0
        self._last_cycle_at: Optional[str] = None
        self._last_cycle_error: Optional[str] = None
        self._cycle_durations: "deque[float]" = deque(maxlen=50)

    def status(self) -> Dict[str, Any]:
        """Cycle count/timing + currently-tracked position count. Report-only,
        same as everything in vesper/metrics.py -- nothing here gates."""
        durations = list(self._cycle_durations)
        return {
            "cycles": self._cycles,
            "last_cycle_at": self._last_cycle_at,
            "last_cycle_error": self._last_cycle_error,
            "tracked_positions": len(self.tracked_positions),
            "last_cycle_duration_sec": round(durations[-1], 3) if durations else None,
            "avg_cycle_duration_sec": round(sum(durations) / len(durations), 3) if durations else None,
        }

    def evaluate_position(
        self,
        pos: MonitoredPosition,
        current_time_et: Optional[datetime] = None,
        spy_spot: Optional[float] = None,
        spy_gamma_flip: Optional[float] = None,
        underlying_technicals: Optional[Dict[str, Any]] = None,
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

        # 5. Underlying-Keyed Swing-Option Stop (200 SMA / 34 EMA / lower
        # Keltner band on the UNDERLYING, not a fixed % on the contract).
        # This is an ADDITIONAL independent trigger alongside the flat -40%
        # stop above -- both stay armed, whichever breaches first exits the
        # position. Only evaluates when the position was actually drafted
        # with a swing stop (underlying_stop_type=="underlying_level") AND
        # this cycle's underlying_technicals read succeeded; a fetch failure
        # or a missing basis key means "skip this cycle", never "passed" or
        # "0.0" -- fail closed, never fabricate a level.
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
                    return ExitTrigger(
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
                return ExitTrigger(
                    position=pos,
                    reason="GAMMA_FLIP_VIOLATION",
                    sell_quantity=pos.quantity,
                    est_proceeds=pos.quantity * pos.current_price * 100,
                    pnl_pct=pnl,
                )

        # 7. Time-Based Exit for 0DTE (>= 3:00 PM ET)
        now_et = current_time_et or datetime.now(timezone.utc)
        if pos.is_0dte and (now_et.hour > self.time_stop_hour_et or (now_et.hour == self.time_stop_hour_et and now_et.minute >= self.time_stop_minute_et)):
            return ExitTrigger(
                position=pos,
                reason="TIME_STOP",
                sell_quantity=pos.quantity,
                est_proceeds=pos.quantity * pos.current_price * 100,
                pnl_pct=pnl,
            )

        # 8. Earnings-Week CSP Vega Harvest Exit (force-close on/after the
        # date the IV crush was expected to have happened -- see
        # OrderProposal.earnings_exit_date). Date-driven, not P&L-driven: the
        # whole point of this trade is harvesting the IV collapse, so it
        # exits on schedule regardless of whether pnl looks good or bad at
        # that moment. Fails closed on a malformed date (skips rather than
        # guessing an exit time) instead of raising and killing the cycle.
        if pos.earnings_exit_date:
            try:
                exit_date = datetime.strptime(pos.earnings_exit_date, "%Y-%m-%d").date()
                if now_et.date() >= exit_date:
                    return ExitTrigger(
                        position=pos,
                        reason="EARNINGS_EXIT",
                        sell_quantity=pos.quantity,
                        est_proceeds=pos.quantity * pos.current_price * (100 if pos.asset_type == "OPTION" else 1),
                        pnl_pct=pnl,
                    )
            except ValueError:
                logger.warning(f"Malformed earnings_exit_date {pos.earnings_exit_date!r} for {pos.symbol}, skipping earnings-exit check")

        return None

    async def poll_webull_positions(self) -> List[MonitoredPosition]:
        """Fetch open positions from Webull portfolio."""
        positions = []
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
                            # underlying_stop_type/basis stay None (their
                            # dataclass default) for every Webull-sourced
                            # position. Webull's own position API returns no
                            # strategy/stop metadata, and this codebase has no
                            # local record tying a live fill back to the
                            # playbook that drafted it -- so a live OPTION
                            # position silently keeps only the flat
                            # contract-pct stop until a local live-fill
                            # record store exists (see ROADMAP.md). This is
                            # the correct "skip when unavailable" behavior,
                            # not an oversight.
                        )
                    )
        except Exception as e:
            logger.warning(f"Could not poll Webull positions: {e}")
        return positions

    def poll_paper_positions(self) -> List[MonitoredPosition]:
        """Fetch open positions from Paper Ledger for dry-run monitoring."""
        positions = []
        try:
            from core.paper_ledger import get_paper_positions
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
                        underlying_stop_type=f.get("underlying_stop_type"),
                        underlying_stop_basis=f.get("underlying_stop_basis"),
                        earnings_exit_date=f.get("earnings_exit_date"),
                    )
                )
        except Exception as e:
            logger.warning(f"Could not poll paper positions: {e}")
        return positions

    async def execute_exit_cascade(self, trigger: ExitTrigger, live: bool = False) -> ExecutionResult:
        """Executes the exit cascade order through ExecutionGuard."""
        pos = trigger.position
        logger.warning(f"🚨 EXIT TRIGGERED for {pos.symbol}: {trigger.reason} (PnL: {trigger.pnl_pct:+.1%})")

        # Metrics: paper-vs-live outcome signal. Digest is over the symbol
        # only (never the fill/quantity/price payload) -- mirrors
        # execution_guard._digest's "hash, never raw payload" pattern.
        _metrics_mode = "live" if live else "paper"
        _metrics_digest = hashlib.sha256(pos.symbol.encode()).hexdigest()[:16]

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
                from core.paper_ledger import get_paper_positions, close_paper_position
                for op in get_paper_positions():
                    if op.get("ticker") == pos.symbol and op.get("status") == "OPEN":
                        close_paper_position(op["id"], close_price=pos.current_price, reason=trigger.reason)
                        break
            except Exception as e:
                logger.warning(f"Could not close paper position for {pos.symbol}: {e}")

            metrics.record_order_outcome(mode=_metrics_mode, status=res.status, broker="webull", payload_digest=_metrics_digest)
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
            # This SELL closes a position monitor.py already tracks as open --
            # the market value (limit_price*100*qty) is the right notional
            # figure here, not the strike. is_closing=True tells the guard
            # not to apply the SELL-to-open strike-based check (see
            # execution_guard.py) meant for a *new* short option position.
            "is_closing": True,
        }

        try:
            from core.wb import Webull
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
            metrics.record_order_outcome(mode=_metrics_mode, status=res.status, broker="webull", payload_digest=_metrics_digest)
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
            metrics.record_order_outcome(mode=_metrics_mode, status=res.status, broker="webull", payload_digest=_metrics_digest)
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
            metrics.record_order_outcome(mode=_metrics_mode, status=res.status, broker="webull", payload_digest=_metrics_digest)
            await channel_manager.broadcast_execution(res)
            return res

    async def run_monitoring_cycle(self, live: bool = False) -> List[ExecutionResult]:
        """Runs a single evaluation sweep across all positions.

        Thin timing wrapper around _run_monitoring_cycle_impl -- see status()
        for why cycle count/duration lives on this instance rather than in
        vesper/metrics.py."""
        start = _monotonic()
        try:
            results = await self._run_monitoring_cycle_impl(live=live)
        except Exception as e:
            self._last_cycle_error = str(e)
            raise
        else:
            self._last_cycle_error = None
            return results
        finally:
            self._cycles += 1
            self._last_cycle_at = datetime.now(timezone.utc).isoformat()
            self._cycle_durations.append(_monotonic() - start)

    async def _run_monitoring_cycle_impl(self, live: bool = False) -> List[ExecutionResult]:
        positions = await self.poll_webull_positions()
        if not live:
            positions.extend(self.poll_paper_positions())
        results = []

        # Get current SPY Gamma Flip
        spy_spot = None
        spy_flip = None
        try:
            from core.td import TDPro
            td = TDPro()
            if td.configured:
                spy_levels = await asyncio.to_thread(td.levels, "SPY")
                spy_spot = float(spy_levels.get("spot", 0.0)) or None
                spy_flip = float(spy_levels.get("gamma_flip", 0.0)) or None
        except Exception:
            pass

        # Fetch underlying technicals for every position drafted with a swing
        # stop (underlying_stop_type=="underlying_level"), once per unique
        # underlying ticker per cycle -- multiple contracts on the same name
        # (e.g. two paper fills on the same LEAPS underlying) share one fetch.
        # analyze_technicals() is @smart_cache'd at 300s/3600s TTL, so calling
        # it every monitor tick (default 15s) does NOT hit yfinance every
        # tick despite the naive per-cycle call site -- the cache absorbs it.
        underlying_tech: Dict[str, Optional[Dict[str, Any]]] = {}
        swing_underlyings = {
            p.symbol for p in positions
            if p.asset_type == "OPTION" and p.underlying_stop_type == "underlying_level"
        }
        if swing_underlyings:
            from core.technicals import analyze_technicals
            for u in swing_underlyings:
                try:
                    res = await analyze_technicals(ticker=u, period="1y")
                    underlying_tech[u] = res.data if hasattr(res, "data") and isinstance(res.data, dict) else None
                except Exception as e:
                    logger.warning(f"Could not fetch underlying technicals for {u}: {e}")
                    underlying_tech[u] = None

        for pos in positions:
            # Update cache
            if pos.symbol not in self.tracked_positions:
                self.tracked_positions[pos.symbol] = pos
            else:
                existing = self.tracked_positions[pos.symbol]
                existing.current_price = pos.current_price
                pos = existing

            trigger = self.evaluate_position(
                pos,
                spy_spot=spy_spot,
                spy_gamma_flip=spy_flip,
                underlying_technicals=underlying_tech.get(pos.symbol),
            )
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
    """Continuous background loop for position monitoring.

    Polls every `interval_sec`, but is also woken immediately by the gRPC
    trade-event feed when one is available (see vesper/stream_runner.py), so a
    fill -- including one placed by hand in Webull Desktop -- is acted on in
    about a second rather than up to a full interval later. If the feed can't
    start, `wake` is simply never set and this degrades to exactly the timer
    behaviour it had before.
    """
    monitor = PositionMonitor()
    print("\n" + "=" * 76)
    print(f"🛡️ VESPER ACTIVE POSITION MONITOR & EXIT CASCADE (Mode: {'LIVE' if live else 'DRY_RUN'})")
    print(f"Rules: Take Profit=+50% | Stop Loss=-40% | Trailing Breakeven=+25% | Time Stop=15:00 ET")

    wake = asyncio.Event()
    pushed = False
    if not once:
        from vesper.stream_runner import start_trade_events

        pushed = await start_trade_events(wake)
    print(
        f"Fills: {'push (gRPC trade events) + ' if pushed else ''}poll every {interval_sec:g}s"
        + ("" if pushed else "  [push unavailable]")
    )
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

        # Sleep until the interval elapses OR a trade event lands, whichever
        # comes first. wait_for on the Event rather than sleep() is what turns
        # a fill into a ~1s reaction instead of a ~15s one.
        try:
            await asyncio.wait_for(wake.wait(), timeout=interval_sec)
            wake.clear()
        except asyncio.TimeoutError:
            pass
