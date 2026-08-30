"""Candidate Discovery & Screener Node."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.state import TradingState, Candidate
from mcp_server.screener import run_stock_screen
from mcp_server.vcp_screener import screen_vcp
from tickertrace_mcp import get_briefing

logger = logging.getLogger(__name__)

# Cap per screener preset. The point is a workable shortlist, not every row --
# analyst_node runs real technicals on each candidate, so an unbounded screener
# would blow up the analysis pass and the 2-req/2s account bucket behind it.
SCREENER_MAX_PER_PRESET = 8


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

    # 4. Unusual Options Flow (TraderDaddy Pro + Flow Classifier)
    # Screens large anomalous options prints and filters them through classify_unusual_activity_record:
    # Promotes DIRECTIONAL prints to Candidate; filters out HEDGE prints (dealer/institutional rebalancing).
    if playbook in ("unusual_flow", "flow", "options_flow", "all"):
        try:
            from td import TDPro
            from vesper.flow_classifier import classify_unusual_activity_record

            td = TDPro()
            if td.configured:
                raw_flow = await asyncio.to_thread(td.cached, "get_unusual_activity", {"limit": 20, "minScore": 70})
                flow_items = []
                if isinstance(raw_flow, dict):
                    flow_items = raw_flow.get("data") or raw_flow.get("items") or []
                elif isinstance(raw_flow, list):
                    flow_items = raw_flow

                regime = state.get("regime")
                spot_price = regime.spy_spot if regime else None
                gamma_flip = regime.spy_gamma_flip if regime else None

                for item in flow_items:
                    ticker = item.get("ticker", "").strip().upper()
                    if not ticker or any(c.ticker == ticker for c in candidates):
                        continue

                    t_spot = spot_price if ticker == "SPY" else None
                    t_flip = gamma_flip if ticker == "SPY" else None

                    classification = classify_unusual_activity_record(
                        record=item,
                        spot_price=t_spot,
                        gamma_flip=t_flip,
                    )

                    if classification == "DIRECTIONAL":
                        candidates.append(
                            Candidate(
                                ticker=ticker,
                                source="UNUSUAL_FLOW",
                                score=8.5,
                                rationale=(
                                    f"Confirmed Directional Options Flow: {item.get('sentimentLabel') or item.get('sentiment', 'Active')} "
                                    f"({item.get('type', 'OPT')} vsOI: {item.get('vsOI', 'N/A')}%, Score: {item.get('score', 'N/A')})"
                                ),
                                data=item,
                            )
                        )
                        audit_notes.append(
                            f"Directional Flow Candidate: {ticker} ({item.get('type')} {item.get('sentiment')})"
                        )
                    elif classification == "HEDGE":
                        audit_notes.append(
                            f"Filtered out hedge flow for {ticker}: classified as institutional/dealer hedge"
                        )
        except Exception as e:
            logger.warning(f"Error scanning unusual options flow: {e}")

    # 5. TraderDaddy screeners — the point of the scanner: it brings tickers
    # to you rather than you curating a watchlist.
    #
    # The screener preset is chosen from the playbook being run, because
    # TDPro's presets map almost 1:1 onto the playbooks: there is no reason to
    # feed CSP-wheel candidates into a 0DTE gamma playbook. On "all", every
    # preset below runs. Confirmed live 2026-08-30: run_screener requires a
    # `screener` arg from a fixed enum, and returns {results: [...]} rows keyed
    # by `symbol` with adx / rsi / stochK / atr / price / volume alongside.
    _SCREENER_FOR_PLAYBOOK = {
        "momentum_squeeze": ["bullish-pullback", "volatility-squeeze"],
        "adx_iv_router": ["momentum", "csp-wheel", "leaps"],
        "collar_following": ["csp-wheel"],
        "thega": ["volatility-surge"],
        "0dte_flow": ["gamma-scan"],
        "leveraged": ["leveraged"],
        "all": ["bullish-pullback", "volatility-squeeze", "momentum", "gamma-scan"],
    }
    for preset in _SCREENER_FOR_PLAYBOOK.get(playbook, []):
        try:
            from td import TDPro

            client = TDPro()
            if not client.configured:
                break
            res = await asyncio.to_thread(client.cached, "run_screener", {"screener": preset})
            rows = res if isinstance(res, list) else next(
                (v for v in (res or {}).values() if isinstance(v, list)), []
            )
            added = 0
            for row in rows[:SCREENER_MAX_PER_PRESET]:
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("symbol") or "").strip().upper()
                if not ticker or any(c.ticker == ticker for c in candidates):
                    continue
                bits = [
                    f"ADX {row['adx']:.0f}" for _ in [0] if isinstance(row.get("adx"), (int, float))
                ] + [
                    f"RSI {row['rsi']:.0f}" for _ in [0] if isinstance(row.get("rsi"), (int, float))
                ]
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        source="TDPRO_SCREENER",
                        score=8.0,
                        rationale=f"TDPro '{preset}' screener"
                                  + (f" ({', '.join(bits)})" if bits else ""),
                        data=row,
                    )
                )
                added += 1
            audit_notes.append(f"TDPro screener '{preset}': {added} candidate(s) from {len(rows)} row(s)")
        except Exception as e:
            logger.warning(f"TDPro screener '{preset}' failed: {e}")
            audit_notes.append(f"TDPro screener '{preset}' unavailable: {e}")

    # 6. Pre-market gappers — only meaningful before/around the open, and the
    # feed says so itself via `session`, so it is not gated on a clock here.
    if playbook in ("all", "momentum_squeeze", "gappers"):
        try:
            from td import TDPro

            client = TDPro()
            if client.configured:
                res = await asyncio.to_thread(client.cached, "get_premarket_gappers", {})
                gappers = (res or {}).get("gappers") or []
                added = 0
                for row in gappers[:SCREENER_MAX_PER_PRESET]:
                    ticker = str(row.get("symbol") or "").strip().upper()
                    if not ticker or any(c.ticker == ticker for c in candidates):
                        continue
                    # `cleared` is the feed's own verdict on whether the gap
                    # passed its pillar checks; failed ones are noise here.
                    if row.get("cleared") is False:
                        continue
                    chg = row.get("changePct")
                    candidates.append(
                        Candidate(
                            ticker=ticker,
                            source="PREMARKET_GAPPER",
                            score=8.5,
                            rationale="Pre-market gapper"
                                      + (f" {chg:+.1f}%" if isinstance(chg, (int, float)) else "")
                                      + (" with news" if row.get("hasNews") else ""),
                            data=row,
                        )
                    )
                    added += 1
                audit_notes.append(f"Pre-market gappers: {added} candidate(s) from {len(gappers)}")
        except Exception as e:
            logger.warning(f"Pre-market gappers failed: {e}")

    # 7. Bounce signals — TDPro's own detector, keyed `ticker` not `symbol`.
    if playbook in ("all", "momentum_squeeze", "bounce"):
        try:
            from td import TDPro

            client = TDPro()
            if client.configured:
                res = await asyncio.to_thread(client.cached, "get_bounce_signals", {})
                signals = (res or {}).get("signals") or []
                added = 0
                for row in signals[:SCREENER_MAX_PER_PRESET]:
                    ticker = str(row.get("ticker") or "").strip().upper()
                    if not ticker or any(c.ticker == ticker for c in candidates):
                        continue
                    candidates.append(
                        Candidate(
                            ticker=ticker,
                            source="TDPRO_BOUNCE",
                            score=8.5,
                            rationale=f"TDPro bounce signal ({row.get('signalType') or 'detected'})",
                            data=row,
                        )
                    )
                    added += 1
                audit_notes.append(f"Bounce signals: {added} candidate(s) from {len(signals)}")
        except Exception as e:
            logger.warning(f"Bounce signals failed: {e}")

    # 8. 0DTE Core Indices
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
