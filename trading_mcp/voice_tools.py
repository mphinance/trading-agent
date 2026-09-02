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


def _audit_voice_tool_call(tool: str, **kwargs: Any) -> None:
    """M8-12: Audit logger for all voice-originated MCP tools.
    Filters credentials by construction and appends entry to core.audit_chain.
    """
    from datetime import datetime, timezone
    from core.audit_chain import append_entry

    clean_args = {}
    for k, v in kwargs.items():
        k_lower = k.lower()
        if any(bad in k_lower for bad in ("token", "secret", "key", "auth", "password", "credential")):
            continue
        clean_args[k] = v

    try:
        append_entry(
            session_id="mcp-voice",
            node=f"voice:{tool}",
            entry={
                "tool": tool,
                "arguments": clean_args,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        logger.warning(f"Voice tool audit append failed for {tool}: {e}")



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
    _audit_voice_tool_call("watch_setup", proposal_id=proposal_id, force_full=force_full)

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
    _audit_voice_tool_call("find_pending_setup", query=query)
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


def snooze_proposal(proposal_id: str, minutes: float = 60.0) -> dict[str, Any]:
    """Annotate a pending proposal with a suppress-until timestamp.

    M8-06. Does NOT alter price, quantity, or approval status ('PENDING').
    The proposal remains fully approvable by button tap at any time.
    """
    _audit_voice_tool_call("snooze_proposal", proposal_id=proposal_id, minutes=minutes)
    from datetime import datetime, timezone, timedelta
    from core.approval_registry import _load_approval_state, _save_approval_state

    state = _load_approval_state()
    pending = state["pending"].get(proposal_id)
    if not pending:
        return {
            "available": False,
            "proposal_id": proposal_id,
            "reason": f"Proposal '{proposal_id}' not found in pending approvals",
        }

    now = datetime.now(timezone.utc)
    suppress_until = now + timedelta(minutes=float(minutes))
    suppress_iso = suppress_until.isoformat()

    # Record suppress timestamp on the proposal without touching price/quantity/status
    pending["suppress_until"] = suppress_iso
    _save_approval_state(state)

    logger.info(f"Snoozed proposal {proposal_id} for {minutes}m until {suppress_iso}")
    return {
        "available": True,
        "proposal_id": proposal_id,
        "status": pending.get("status", "PENDING"),
        "snoozed_minutes": minutes,
        "suppress_until": suppress_iso,
        "message": f"Proposal {proposal_id} snoozed until {suppress_iso}",
    }


def tag_proposal(proposal_id: str, note: str) -> dict[str, Any]:
    """Append a short text note to a pending proposal and record it in the audit trail.

    M8-06. Appends to notes without modifying price, quantity, or invoking submit_decision.
    """
    _audit_voice_tool_call("tag_proposal", proposal_id=proposal_id, note=note)
    from datetime import datetime, timezone
    from core.approval_registry import _load_approval_state, _save_approval_state
    from core.audit_chain import append_entry

    clean_note = (note or "").strip()
    if not clean_note:
        return {"available": False, "proposal_id": proposal_id, "reason": "Note must be non-empty"}

    state = _load_approval_state()
    pending = state["pending"].get(proposal_id)
    if not pending:
        return {
            "available": False,
            "proposal_id": proposal_id,
            "reason": f"Proposal '{proposal_id}' not found in pending approvals",
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    note_entry = {"timestamp": now_iso, "note": clean_note}

    # Append to proposal's notes list
    notes = pending.setdefault("notes", [])
    notes.append(note_entry)
    _save_approval_state(state)

    # Append to hash-chained tamper-evident audit ledger
    session_id = pending.get("session_id", "mcp-voice")
    append_entry(
        session_id=session_id,
        node="tag_proposal",
        entry={"proposal_id": proposal_id, "note": clean_note, "timestamp": now_iso},
    )

    logger.info(f"Tagged proposal {proposal_id}: {clean_note}")
    return {
        "available": True,
        "proposal_id": proposal_id,
        "notes": notes,
        "latest_note": note_entry,
    }


def _get_alert_store():
    import alerts
    path = getattr(alerts, "STORE_PATH", None)
    return alerts.AlertStore(path) if path else alerts.AlertStore()


def arm_alert(
    symbol: str,
    level: str | float,
    direction: str,
    note: str = "",
    repeat: bool = False,
) -> dict[str, Any]:
    """Arm a price or dealer-gamma alert in the shared alert store.

    M8-07. Level can be static number or dynamic reference ('flip', 'pin', 'wall_above', 'wall_below').
    """
    _audit_voice_tool_call("arm_alert", symbol=symbol, level=level, direction=direction, note=note, repeat=repeat)
    from alerts import make_alert, AlertError

    try:
        alert = make_alert(
            symbol=symbol,
            level=level,
            direction=direction,
            note=note,
            repeat=repeat,
        )
        store = _get_alert_store()
        saved = store.add(alert)
        return {
            "available": True,
            "alert_id": saved["id"],
            "symbol": saved["symbol"],
            "level_ref": saved["level_ref"],
            "level_static": saved["level_static"],
            "direction": saved["direction"],
            "state": saved["state"],
        }
    except AlertError as e:
        return {"available": False, "reason": str(e)}
    except Exception as e:
        return {"available": False, "reason": f"Failed to arm alert: {e}"}


def disarm_alert(alert_id: str) -> dict[str, Any]:
    """Disarm and remove an alert from the shared alert store.

    M8-07. Once removed, the alert no longer evaluates.
    """
    _audit_voice_tool_call("disarm_alert", alert_id=alert_id)
    try:
        store = _get_alert_store()
        removed = store.remove(alert_id)
        return {
            "available": True,
            "disarmed": removed,
            "alert_id": alert_id,
        }
    except Exception as e:
        return {"available": False, "alert_id": alert_id, "reason": str(e)}


def halt(reason: str = "Emergency halt via MCP", source: str = "mcp") -> dict[str, Any]:
    """Trigger an immediate emergency halt, freezing all execution paths.

    M8-08. Flips the persistent halt state via core.halt.halt().
    """
    _audit_voice_tool_call("halt", reason=reason, source=source)
    from core.halt import halt as _core_halt

    return _core_halt(reason=reason, source=source)


_find_pending_setup_impl = find_pending_setup
_watch_setup_impl = watch_setup
_snooze_proposal_impl = snooze_proposal
_tag_proposal_impl = tag_proposal
_arm_alert_impl = arm_alert
_disarm_alert_impl = disarm_alert
_halt_impl = halt


def register_voice_tools(mcp: Any) -> list[str]:
    """Register voice co-pilot watch and safe-write tools onto `mcp` and return their names."""
    from fastmcp.server.auth import require_scopes

    read_auth = require_scopes("read")
    safe_write_auth = require_scopes("safe-write")

    @mcp.tool(auth=read_auth)
    def watch_setup(proposal_id: str, force_full: bool = False) -> dict[str, Any]:
        """Inspect a pending trade setup with concise voice-speakable telemetry (<2KB).

        Returns thesis, entry/stop/target, distance to trigger (% and $),
        5-minute bar structure phrase, VWAP relation, and compact dealer gamma.
        Repeated calls within TTL with unchanged market state return compressed delta.
        """
        return _watch_setup_impl(proposal_id, force_full=force_full)

    @mcp.tool(auth=read_auth)
    def find_pending_setup(query: str) -> dict[str, Any]:
        """Fuzzy resolve a spoken or mis-transcribed symbol against pending proposals.

        Echoes back the resolved ticker symbol and proposal_id, or flags ambiguity.
        """
        return _find_pending_setup_impl(query)

    @mcp.tool(auth=safe_write_auth)
    def snooze_proposal(proposal_id: str, minutes: float = 60.0) -> dict[str, Any]:
        """Snooze a pending proposal for `minutes` without modifying its price, quantity, or approval state."""
        return _snooze_proposal_impl(proposal_id, minutes=minutes)

    @mcp.tool(auth=safe_write_auth)
    def tag_proposal(proposal_id: str, note: str) -> dict[str, Any]:
        """Append a note to a pending proposal visible in queue and audit trail, without deciding it."""
        return _tag_proposal_impl(proposal_id, note=note)

    @mcp.tool(auth=safe_write_auth)
    def arm_alert(
        symbol: str,
        level: str | float,
        direction: str,
        note: str = "",
        repeat: bool = False,
    ) -> dict[str, Any]:
        """Arm a price or gamma alert (flip/pin/wall_above/wall_below) in the shared alert store."""
        return _arm_alert_impl(symbol, level, direction, note=note, repeat=repeat)

    @mcp.tool(auth=safe_write_auth)
    def disarm_alert(alert_id: str) -> dict[str, Any]:
        """Disarm and remove an alert from the store."""
        return _disarm_alert_impl(alert_id)

    @mcp.tool(auth=safe_write_auth)
    def halt(reason: str = "Emergency halt via MCP", source: str = "mcp") -> dict[str, Any]:
        """Trigger an immediate emergency halt, freezing all execution paths."""
        return _halt_impl(reason=reason, source=source)

    return [
        "watch_setup",
        "find_pending_setup",
        "snooze_proposal",
        "tag_proposal",
        "arm_alert",
        "disarm_alert",
        "halt",
    ]

