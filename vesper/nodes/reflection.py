"""Post-Trade Reflection & Thesis Journaling Node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.state import TradingState
from mcp_server.conviction import log_conviction

logger = logging.getLogger(__name__)


async def reflection_node(state: TradingState) -> Dict[str, Any]:
    """Records trade thesis, conviction score, and post-session reflection."""
    logger.info("-> [ReflectionNode] Logging conviction & journaling session...")
    
    execution_results = state.get("execution_results", [])
    technicals = state.get("technicals", {})
    regime = state.get("regime")
    reflection_notes: List[str] = []
    
    for res in execution_results:
        ticker = res.ticker
        tech = technicals.get(ticker)
        
        thesis_text = (
            f"Session: {state.get('session_id')} | Playbook: {state.get('selected_playbook')} | "
            f"Status: {res.status} | Regime: {regime.posture if regime else 'N/A'}"
        )
        
        if tech:
            thesis_text += f" | RSI: {tech.rsi_14:.1f} | EMA Stack: {tech.ema_stack}"
            
        try:
            # Log conviction into project memory
            await log_conviction(
                ticker=ticker,
                direction="bullish" if "BUY" in str(res.message) else "bearish",
                confidence=4,
                reasoning=thesis_text,
                signals=f"RSI_{tech.rsi_state if tech else 'N/A'},EMA_{tech.ema_stack if tech else 'N/A'}",
            )
            reflection_notes.append(f"Recorded conviction thesis for {ticker} ({res.status})")
        except Exception as e:
            logger.warning(f"Failed to log conviction for {ticker}: {e}")
            reflection_notes.append(f"Thesis note for {ticker}: {thesis_text}")

    audit_entry = {
        "node": "reflection_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "notes": reflection_notes,
    }

    return {
        "reflection_notes": reflection_notes,
        "audit_trail": [audit_entry],
    }
