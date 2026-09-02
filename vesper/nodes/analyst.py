"""Deep Technical Audit & Options VoPR™ Engine Node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Any

from vesper.state import TradingState, TechnicalAudit, OptionAudit
from core.technicals import analyze_technicals
from core.options import find_best_to_buy, analyze_options_setup

logger = logging.getLogger(__name__)


async def analyst_node(state: TradingState) -> Dict[str, Any]:
    """Runs indicator audits and VoPR™ pricing for candidate tickers."""
    candidates = state.get("candidates", [])
    logger.info(f"-> [AnalystNode] Analyzing {len(candidates)} candidate(s)...")

    technicals_dict: Dict[str, TechnicalAudit] = {}
    options_dict: Dict[str, OptionAudit] = {}
    audit_notes = []

    for c in candidates[:3]:  # Top 3 candidates
        t = c.ticker
        try:
            # 1. Technical Indicators
            tech_res = await analyze_technicals(ticker=t, period="1y")
            tech_data = tech_res.data if hasattr(tech_res, "data") and isinstance(tech_res.data, dict) else {}
            
            close = float(tech_data.get("close", 0.0))
            rsi_val = float(tech_data.get("rsi_14", 50.0))
            rsi_state = str(tech_data.get("rsi_14_state", "neutral"))
            macd_sig = str(tech_data.get("macd_signal", "NEUTRAL"))
            ema_stack = str(tech_data.get("ema_stack_status", "NEUTRAL"))

            tech_audit = TechnicalAudit(
                ticker=t,
                close=close,
                rsi_14=rsi_val,
                rsi_state=rsi_state,
                macd_signal=macd_sig,
                ema_stack=ema_stack,
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
                summary=tech_data.get("summary", f"{t}: Close={close}, RSI={rsi_val:.1f}"),
            )
            technicals_dict[t] = tech_audit
            audit_notes.append(f"{t}: RSI={rsi_val:.1f}, EMA={ema_stack}")

            # 2. Options VoPR™ Setup
            opt_res = await find_best_to_buy(ticker=t, budget=1000.0)
            if isinstance(opt_res, dict) and "recommendations" in opt_res:
                recs = opt_res.get("recommendations", [])
                if recs:
                    best = recs[0]
                    options_dict[t] = OptionAudit(
                        ticker=t,
                        option_type=best.get("option_type", "call"),
                        strike=float(best.get("strike", close)),
                        expiry=str(best.get("expiry", "")),
                        dte=int(best.get("dte", 30)),
                        delta=float(best.get("delta", 0.5)),
                        theta=float(best.get("theta", 0.0)),
                        iv=float(best.get("iv", 0.0)),
                        vopr_grade=best.get("grade", "B"),
                        return_on_capital=best.get("estimated_roc"),
                        recommendation=best.get("rationale", ""),
                    )
                    audit_notes.append(f"{t} Option: Grade {best.get('grade')}, {best.get('option_type').upper()} Strike={best.get('strike')}")
        except Exception as e:
            logger.warning(f"Error auditing {t} in AnalystNode: {e}")

    audit_entry = {
        "node": "analyst_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "analyzed_tickers": list(technicals_dict.keys()),
        "notes": audit_notes,
    }

    return {
        "technicals": technicals_dict,
        "options_audits": options_dict,
        "audit_trail": [audit_entry],
    }
