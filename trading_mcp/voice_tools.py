"""trading_mcp.voice_tools: voice co-pilot watch surface for pending trade setups.

M8-01: watch_setup(proposal_id) - small speakable payload (<2KB) with thesis,
       entry/stop/target, distance to trigger, 5-minute bar structure, VWAP relation,
       and compacted dealer-gamma levels.
M8-04: Repeat-call suppression cache - if asked repeatedly within a short TTL
       with price and structure unchanged, returns a compressed 1-line update.
M8-05: find_pending_setup(query) - fuzzy matches spoken / mistranscribed symbols
       (e.g. 'in video' -> NVDA) against pending proposals and echoes resolution.
"""

from __future__ import annotations

import difflib
import json
import logging
import time
from typing import Any, Mapping

from core.approval_registry import approval_registry
from trading_mcp.bar_summary import summarize_bars_for_voice, _get_val
from trading_mcp.gamma_summary import get_compact_gamma

logger = logging.getLogger(__name__)

# Common spoken / speech-to-text mistranscriptions mapped to ticker symbols
SPOKEN_SYMBOL_ALIASES: dict[str, str] = {
    "in video": "NVDA",
    "invideo": "NVDA",
    "nvidia": "NVDA",
    "invidia": "NVDA",
    "envidia": "NVDA",
    "tesla": "TSLA",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "google": "GOOGL",
    "meta": "META",
    "spy": "SPY",
    "qqq": "QQQ",
    "iwm": "IWM",
}

# M8-04: In-memory cache for repeat-call suppression
_WATCH_CACHE: dict[str, dict[str, Any]] = {}
CACHE_TTL_SECONDS = 90.0
PRICE_MOVE_THRESHOLD = 0.0025  # 0.25%
MAX_CACHE_SIZE = 100


def _num(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get_market_client():
    from core.wb import Webull
    from core.md import Market

    wb = Webull()
    if wb.configured:
        return Market(wb)
    return Market(wb)


def _compute_vwap_relation(bars: list[Any], current_price: float | None) -> dict[str, Any]:
    """Compute VWAP from bars and calculate distance and position relative to spot."""
    if not bars or current_price is None:
        return {"available": False, "phrase": "VWAP unavailable"}

    cum_pv = 0.0
    cum_vol = 0.0
    for b in bars:
        h = _get_val(b, "high", "h")
        l = _get_val(b, "low", "l")
        c = _get_val(b, "close", "c", "last")
        v = _get_val(b, "volume", "v", "vol")
        typical = (h + l + c) / 3.0 if (h or l or c) else c
        cum_pv += typical * v
        cum_vol += v

    if cum_vol <= 0:
        return {"available": False, "phrase": "VWAP unavailable (zero volume)"}

    vwap = cum_pv / cum_vol
    diff_dollars = current_price - vwap
    diff_pct = (diff_dollars / vwap) * 100.0

    if abs(diff_pct) < 0.1:
        phrase = f"spot is flat with VWAP ({vwap:.2f})"
    elif diff_pct > 0:
        phrase = f"spot is {diff_pct:.1f}% above VWAP ({vwap:.2f})"
    else:
        phrase = f"spot is {abs(diff_pct):.1f}% below VWAP ({vwap:.2f})"

    return {
        "available": True,
        "vwap": round(vwap, 2),
        "diff_dollars": round(diff_dollars, 2),
        "diff_pct": round(diff_pct, 2),
        "phrase": phrase,
    }


def watch_setup(proposal_id: str, force_full: bool = False) -> dict[str, Any]:
    """Inspect a pending proposal with voice-tailored market context (<2KB).

    M8-01 / M8-04. Sourced from core.approval_registry, core.md, and core.td.
    """
    # 1. Proposal lookup
    pending = approval_registry.get_pending(proposal_id)
    if not pending:
        return {
            "available": False,
            "proposal_id": proposal_id,
            "reason": f"Proposal '{proposal_id}' not found in pending approvals",
        }

    details = pending.get("details") or {}
    symbol = (details.get("ticker") or details.get("symbol") or "").upper()
    side = (details.get("side") or "BUY").upper()
    is_long = side in ("BUY", "LONG")

    entry = _num(details.get("entry") or details.get("limit_price") or details.get("price"))
    stop = _num(details.get("stop") or details.get("stop_loss"))
    target = _num(details.get("target") or details.get("take_profit"))
    thesis = details.get("thesis") or pending.get("thesis") or "No thesis provided"

    # 2. Market data via core.md (with graceful partial degradation)
    market_data_ok = True
    current_price: float | None = None
    bars: list[Any] = []
    structure_info: dict[str, Any] = {"available": False, "phrase": "market data unavailable"}
    vwap_info: dict[str, Any] = {"available": False, "phrase": "VWAP unavailable"}

    try:
        md = _get_market_client()
        snap = md.snapshot([symbol]) if md else {}
        if snap and symbol in snap and snap[symbol].get("last"):
            current_price = _num(snap[symbol]["last"])

        raw_bars = md.bars(symbol, timespan="M5", count=30) if md else []
        if isinstance(raw_bars, list):
            bars = raw_bars
        elif isinstance(raw_bars, dict) and "data" in raw_bars:
            bars = raw_bars["data"]

        if bars and current_price is None:
            current_price = _get_val(bars[-1], "close", "last")
    except Exception as e:
        market_data_ok = False
        logger.warning(f"core.md error for symbol {symbol}: {e}")

    if market_data_ok and bars:
        bar_sum = summarize_bars_for_voice(bars)
        structure_info = {
            "available": True,
            "phrase": bar_sum.phrase,
            "consecutive_higher_lows": bar_sum.get("consecutive_higher_lows", 0),
            "consecutive_lower_lows": bar_sum.get("consecutive_lower_lows", 0),
            "volume_ratio": bar_sum.get("volume_ratio", 1.0),
            "range_direction": bar_sum.get("range_direction", "flat"),
        }
        vwap_info = _compute_vwap_relation(bars, current_price)

    # 3. Distance to trigger calculation
    dist_dollars: float | None = None
    dist_pct: float | None = None
    dist_phrase = "distance to trigger unavailable"
    if current_price is not None and entry is not None and entry > 0:
        if is_long:
            dist_dollars = entry - current_price
            dist_pct = (dist_dollars / entry) * 100.0
            if current_price < entry:
                dist_phrase = f"{abs(dist_pct):.1f}% under the trigger (${abs(dist_dollars):.2f})"
            else:
                dist_phrase = f"triggered ({abs(dist_pct):.1f}% above entry)"
        else:  # SHORT setup
            dist_dollars = current_price - entry
            dist_pct = (dist_dollars / entry) * 100.0
            if current_price > entry:
                dist_phrase = f"{abs(dist_pct):.1f}% above the trigger (${abs(dist_dollars):.2f})"
            else:
                dist_phrase = f"triggered ({abs(dist_pct):.1f}% below entry)"

    # 4. Dealer Gamma from core.td
    gamma = get_compact_gamma(symbol)

    # 5. M8-04 Repeat-call suppression check
    now = time.monotonic()
    cached = _WATCH_CACHE.get(proposal_id)
    if not force_full and cached is not None and current_price is not None:
        age = now - cached["timestamp"]
        price_delta_pct = (
            abs(current_price - cached["price"]) / cached["price"]
            if cached["price"]
            else 1.0
        )
        struct_match = structure_info.get("phrase") == cached["structure_phrase"]

        if age < CACHE_TTL_SECONDS and price_delta_pct < PRICE_MOVE_THRESHOLD and struct_match:
            return {
                "available": True,
                "unchanged": True,
                "proposal_id": proposal_id,
                "symbol": symbol,
                "side": side,
                "current_price": current_price,
                "distance_to_trigger_dollars": round(dist_dollars, 2) if dist_dollars is not None else None,
                "distance_to_trigger_pct": round(dist_pct, 2) if dist_pct is not None else None,
                "distance_phrase": dist_phrase,
                "speakable_summary": f"{symbol} {side} unchanged, {dist_phrase}.",
            }

    # Record snapshot in cache
    if len(_WATCH_CACHE) >= MAX_CACHE_SIZE:
        oldest_k = next(iter(_WATCH_CACHE))
        _WATCH_CACHE.pop(oldest_k, None)

    _WATCH_CACHE[proposal_id] = {
        "timestamp": now,
        "price": current_price,
        "structure_phrase": structure_info.get("phrase", ""),
        "gamma_summary": gamma.get("summary_phrase", ""),
    }

    # Assemble full voice payload
    speakable_parts = [f"{symbol} {side} proposal", dist_phrase]
    if structure_info.get("available"):
        speakable_parts.append(structure_info["phrase"])
    if vwap_info.get("available"):
        speakable_parts.append(vwap_info["phrase"])
    if gamma.get("available"):
        speakable_parts.append(gamma["summary_phrase"])

    response: dict[str, Any] = {
        "available": True,
        "unchanged": False,
        "proposal_id": proposal_id,
        "symbol": symbol,
        "side": side,
        "thesis": thesis,
        "entry": entry,
        "stop": stop,
        "target": target,
        "price_available": current_price is not None,
        "current_price": current_price,
        "distance_to_trigger_dollars": round(dist_dollars, 2) if dist_dollars is not None else None,
        "distance_to_trigger_pct": round(dist_pct, 2) if dist_pct is not None else None,
        "distance_phrase": dist_phrase,
        "structure_summary": structure_info,
        "vwap_relation": vwap_info,
        "gamma": gamma,
        "speakable_summary": ". ".join(speakable_parts) + ".",
    }

    # Verify payload stays under 2KB
    serialized = json.dumps(response)
    if len(serialized) > 2048:
        # Compact gamma walls further if near boundary
        if "walls" in response["gamma"]:
            response["gamma"]["walls"] = response["gamma"]["walls"][:1]

    return response


def find_pending_setup(query: str) -> dict[str, Any]:
    """Fuzzy-resolve a spoken symbol against pending proposals.

    M8-05. Echoes resolved symbol on clean matches or flags explicit ambiguity/no-match.
    """
    cleaned_query = (query or "").strip().lower()
    if not cleaned_query:
        return {"available": False, "query": query, "reason": "empty_query"}

    pending_list = approval_registry.list_pending()
    if not pending_list:
        return {"available": False, "query": query, "reason": "no_pending_proposals"}

    # Direct alias resolution (e.g. 'in video' -> 'NVDA')
    target_symbol = SPOKEN_SYMBOL_ALIASES.get(cleaned_query)

    scored: list[tuple[float, dict[str, Any], str]] = []
    for p in pending_list:
        details = p.get("details") or {}
        sym = (details.get("ticker") or details.get("symbol") or "").upper()
        if not sym:
            continue

        if target_symbol and sym == target_symbol:
            score = 1.0
        elif sym.lower() == cleaned_query:
            score = 1.0
        else:
            # Sequence matcher similarity
            ratio = difflib.SequenceMatcher(None, cleaned_query, sym.lower()).ratio()
            # Also check if query is substring
            if cleaned_query in sym.lower() or sym.lower() in cleaned_query:
                ratio = max(ratio, 0.75)
            score = ratio

        if score >= 0.60:
            scored.append((score, p, sym))

    if not scored:
        return {
            "available": False,
            "query": query,
            "reason": "no_match",
        }

    scored.sort(key=lambda x: x[0], reverse=True)

    # Check for ambiguity: if top two are within 0.10 of each other and both > 0.65
    if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 0.10:
        matches = [
            {"proposal_id": s[1].get("proposal_id"), "symbol": s[2], "score": round(s[0], 2)}
            for s in scored[:3]
        ]
        return {
            "available": True,
            "ambiguous": True,
            "query": query,
            "reason": "ambiguous_match",
            "matches": matches,
        }

    best_score, best_proposal, best_sym = scored[0]
    return {
        "available": True,
        "ambiguous": False,
        "query": query,
        "resolved_symbol": best_sym,
        "proposal_id": best_proposal.get("proposal_id"),
        "match_confidence": round(best_score, 2),
        "proposal": best_proposal,
    }


_find_pending_setup_impl = find_pending_setup
_watch_setup_impl = watch_setup


def register_voice_tools(mcp: Any) -> list[str]:
    """Register voice co-pilot watch tools onto `mcp` and return their names."""

    @mcp.tool()
    def watch_setup(proposal_id: str, force_full: bool = False) -> dict[str, Any]:
        """Inspect a pending trade setup with concise voice-speakable telemetry (<2KB).

        Returns thesis, entry/stop/target, distance to trigger (% and $),
        5-minute bar structure phrase, VWAP relation, and compact dealer gamma.
        Repeated calls within TTL with unchanged market state return compressed delta.
        """
        return _watch_setup_impl(proposal_id, force_full=force_full)

    @mcp.tool()
    def find_pending_setup(query: str) -> dict[str, Any]:
        """Fuzzy resolve a spoken or mis-transcribed symbol against pending proposals.

        Echoes back the resolved ticker symbol and proposal_id, or flags ambiguity.
        """
        return _find_pending_setup_impl(query)

    return ["watch_setup", "find_pending_setup"]

