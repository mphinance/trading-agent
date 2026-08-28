"""Candidate Discovery & Screener Node."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.state import TradingState, Candidate
from mcp_server.screener import run_stock_screen
from mcp_server.vcp_screener import screen_vcp
from tickertrace_mcp import get_briefing

logger = logging.getLogger(__name__)


async def scanner_node(state: TradingState) -> Dict[str, Any]:
    """Discovers high-probability setups from multiple uncorrelated sources."""
    logger.info("-> [ScannerNode] Running multi-source setup discovery...")
    
    candidates: List[Candidate] = []
    audit_notes = []
    
    # 1. Direct Target Ticker if provided
    if state.get("target_ticker"):
        t = state["target_ticker"].strip().upper()
        candidates.append(
            Candidate(
                ticker=t,
                source="USER_SPECIFIED",
                score=10.0,
                rationale="Direct ticker request for quantitative audit.",
            )
        )
        audit_notes.append(f"Target ticker specified: {t}")
        return {
            "candidates": candidates,
            "audit_trail": [{
                "node": "scanner_node",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "discovered": [c.ticker for c in candidates],
                "notes": audit_notes,
            }]
        }

    playbook = state.get("selected_playbook", "all")

    # 2. VCP & Squeeze Screening
    if playbook in ("momentum_squeeze", "all"):
        try:
            vcp_res = await screen_vcp(max_tickers=10)
            if hasattr(vcp_res, "data") and isinstance(vcp_res.data, list):
                for item in vcp_res.data[:3]:
                    ticker = item.get("ticker", "")
                    if ticker and not any(c.ticker == ticker for c in candidates):
                        candidates.append(
                            Candidate(
                                ticker=ticker,
                                source="VCP",
                                score=8.5,
                                rationale=f"VCP contraction score: {item.get('score', 'N/A')}",
                                data=item,
                            )
                        )
                        audit_notes.append(f"VCP setup: {ticker}")
        except Exception as e:
            logger.warning(f"Error scanning VCP: {e}")

        try:
            screen_res = await run_stock_screen(preset="bullish_ema_stack", limit=5)
            items = screen_res if isinstance(screen_res, list) else screen_res.get("data", [])
            if isinstance(items, list):
                for item in items[:3]:
                    ticker = item.get("ticker", "")
                    if ticker and not any(c.ticker == ticker for c in candidates):
                        candidates.append(
                            Candidate(
                                ticker=ticker,
                                source="SQUEEZE",
                                score=8.0,
                                rationale="Confirmed Bullish EMA Stack (8/21/34/55/89)",
                                data=item,
                            )
                        )
                        audit_notes.append(f"Bullish EMA Stack: {ticker}")
        except Exception as e:
            logger.warning(f"Error scanning momentum: {e}")

    # 3. Institutional Convergence (TickerTrace)
    if playbook in ("institutional_convergence", "all"):
        try:
            briefing = get_briefing()
            if isinstance(briefing, dict):
                top_buys = briefing.get("topBuys", [])
                for b in top_buys[:3]:
                    ticker = b.get("ticker", "")
                    if ticker and not any(c.ticker == ticker for c in candidates):
                        candidates.append(
                            Candidate(
                                ticker=ticker,
                                source="WHALE_CONVERGENCE",
                                score=9.0,
                                rationale=f"Institutional Net Buying: {b.get('providers', [])}",
                                data=b,
                            )
                        )
                        audit_notes.append(f"Whale Buy: {ticker}")
        except Exception as e:
            logger.warning(f"Error scanning TickerTrace briefing: {e}")

    # 4. 0DTE Core Indices
    if playbook in ("0dte_flow", "all"):
        for index_ticker in ("SPY", "QQQ"):
            if not any(c.ticker == index_ticker for c in candidates):
                candidates.append(
                    Candidate(
                        ticker=index_ticker,
                        source="0DTE_FLOW",
                        score=9.5,
                        rationale="Liquid Index for 0DTE Gamma Flip & Apex Level Trading.",
                    )
                )

    audit_entry = {
        "node": "scanner_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "discovered_count": len(candidates),
        "discovered": [c.ticker for c in candidates],
        "notes": audit_notes,
    }

    return {
        "candidates": candidates,
        "audit_trail": [audit_entry],
    }
