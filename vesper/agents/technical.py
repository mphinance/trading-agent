"""Technical Analysis Specialist Agent."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from vesper.agents.base import BaseSpecialistAgent
from vesper.state import TradingState, WorkerReport, TechnicalAudit

logger = logging.getLogger(__name__)


class TechnicalAnalystAgent(BaseSpecialistAgent):
    """Specialist agent analyzing price action, momentum, trend stacks, and technical setups."""

    name: str = "technical_agent"
    specialty: str = "technical_analysis"

    async def analyze(self, ticker: str, state: TradingState) -> WorkerReport:
        # Check if technical audit already exists in state, otherwise try to compute
        technicals: Dict[str, TechnicalAudit] = state.get("technicals", {})
        audit = technicals.get(ticker)

        if audit is None:
            try:
                from mcp_server.technicals import analyze_technicals
                tech_res = await analyze_technicals(ticker=ticker, period="1y")
                tech_data = tech_res.data if hasattr(tech_res, "data") and isinstance(tech_res.data, dict) else {}
                close = float(tech_data.get("close", 0.0))
                audit = TechnicalAudit(
                    ticker=ticker,
                    close=close,
                    rsi_14=float(tech_data.get("rsi_14", 50.0)),
                    rsi_state=str(tech_data.get("rsi_14_state", "neutral")),
                    macd_signal=str(tech_data.get("macd_signal", "NEUTRAL")),
                    ema_stack=str(tech_data.get("ema_stack_status", "NEUTRAL")),
                    ema_8=tech_data.get("ema_8"),
                    ema_21=tech_data.get("ema_21"),
                    ema_34=tech_data.get("ema_34"),
                    ema_55=tech_data.get("ema_55"),
                    ema_89=tech_data.get("ema_89"),
                    atr_14=tech_data.get("atr_14"),
                    adx_14=tech_data.get("adx_14"),
                    rsi_2=tech_data.get("rsi_2"),
                    rsi_2_prev=tech_data.get("rsi_2_prev"),
                    slow_k=tech_data.get("slow_k"),
                    slow_d=tech_data.get("slow_d"),
                    keltner_lower=tech_data.get("keltner_lower"),
                    keltner_basis=tech_data.get("keltner_basis"),
                    keltner_upper=tech_data.get("keltner_upper"),
                    sma_200=tech_data.get("sma_200"),
                    summary=tech_data.get("summary", f"{ticker}: Close={close}"),
                )
            except Exception as e:
                logger.warning(f"[{self.name}] Failed to compute technicals for {ticker}: {e}")

        if audit is None:
            return WorkerReport(
                agent_name=self.name,
                ticker=ticker,
                direction="NEUTRAL",
                confidence_score=30.0,
                time_horizon="SWING_SHORT",
                thesis_summary=f"No technical data available for {ticker}",
            )

        score = 50.0
        catalysts: List[str] = []
        invalidation_levels: List[float] = []

        # 1. EMA Stack & Trend evaluation
        if audit.ema_stack == "BULLISH":
            score += 20.0
            catalysts.append("Bullish Full EMA Stack (8 > 21 > 34 > 55 > 89)")
        elif audit.ema_stack == "BEARISH":
            score -= 20.0
            catalysts.append("Bearish EMA Stack breakdown")

        # 2. RSI / Momentum
        if audit.rsi_14 > 50.0:
            score += 10.0
        else:
            score -= 10.0

        if audit.rsi_2 is not None and audit.rsi_2 < 10.0:
            score += 15.0
            catalysts.append(f"Extreme Oversold RSI(2) Bounce Setup ({audit.rsi_2:.1f})")

        # 3. MACD
        if str(audit.macd_signal).upper() in ("BULLISH", "BUY"):
            score += 10.0
            catalysts.append("MACD Bullish Histogram / Signal")
        elif str(audit.macd_signal).upper() in ("BEARISH", "SELL"):
            score -= 10.0

        # Invalidation / Stop level calculation
        close = audit.close
        if audit.ema_34 and audit.ema_34 < close:
            invalidation_levels.append(round(audit.ema_34, 2))
        if audit.sma_200 and audit.sma_200 < close:
            invalidation_levels.append(round(audit.sma_200, 2))
        if audit.keltner_lower and audit.keltner_lower < close:
            invalidation_levels.append(round(audit.keltner_lower, 2))
        if not invalidation_levels and audit.atr_14 and close > 0:
            invalidation_levels.append(round(close - (1.5 * audit.atr_14), 2))

        # Determine direction
        if score >= 65.0:
            direction = "BULLISH"
            suggested_playbook = "tao_bounce" if (audit.rsi_2 and audit.rsi_2 < 15) else "momentum_squeeze"
        elif score <= 35.0:
            direction = "BEARISH"
            suggested_playbook = "volatility_harvester"
        else:
            direction = "NEUTRAL"
            suggested_playbook = "collar_following"

        confidence = max(10.0, min(95.0, score if direction == "BULLISH" else (100.0 - score if direction == "BEARISH" else 50.0)))

        return WorkerReport(
            agent_name=self.name,
            ticker=ticker,
            direction=direction,
            confidence_score=round(confidence, 1),
            time_horizon="SWING_SHORT",
            key_catalysts=catalysts,
            invalidation_levels=invalidation_levels,
            thesis_summary=f"{direction} technical posture with {len(catalysts)} catalyst(s). Close={close:.2f}, RSI14={audit.rsi_14:.1f}",
            suggested_playbook=suggested_playbook,
            data={"close": close, "rsi_14": audit.rsi_14, "ema_stack": audit.ema_stack, "macd": str(audit.macd_signal)},
        )
