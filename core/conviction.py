"""
Conviction Journal — Sam's Metacognitive Self-Tracking.

Sam logs every directional call she makes. A resolver later checks
whether she was right. Over time this builds a calibration curve so
Sam can say: "My hit rate on oversold bounces is 67% over 23 calls."

Storage: flat JSON at data/conviction_journal.json
(structured data → filtering, not vector search)
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_JOURNAL_PATH = _DATA_DIR / "conviction_journal.json"

# WIN/LOSS thresholds (percentage move)
WIN_THRESHOLD_PCT = 1.5   # ≥1.5% move in predicted direction = WIN
LOSS_THRESHOLD_PCT = 1.5  # ≥1.5% move against prediction = LOSS

# Resolution horizons (in calendar days — checked against entry age)
RESOLUTION_HORIZONS = [1, 5, 10]

# Minimum age (hours) before first resolution attempt
MIN_AGE_HOURS = 20  # ~1 trading day


# ---------------------------------------------------------------------------
# Journal I/O & Deduplication
# ---------------------------------------------------------------------------

def deduplicate_journal_entries(entries: list[dict]) -> list[dict]:
    """Deduplicate journal entries preserving chronological order.

    An entry is considered duplicate if its exact ID or its semantic tuple
    (ticker, entry_date, session_id, reasoning) has already been seen.
    """
    seen_ids = set()
    seen_payloads = set()
    deduped = []

    for entry in entries:
        eid = entry.get("id")
        ticker = entry.get("ticker")
        entry_date = entry.get("entry_date")
        reasoning = entry.get("reasoning")
        session_id = entry.get("session_id")

        payload_key = (ticker, entry_date, session_id, reasoning)

        if eid and eid in seen_ids:
            continue
        if payload_key in seen_payloads:
            continue

        if eid:
            seen_ids.add(eid)
        seen_payloads.add(payload_key)
        deduped.append(entry)

    return deduped


def _load_journal() -> list[dict]:
    """Load the conviction journal from disk with automatic deduplication."""
    if not _JOURNAL_PATH.exists():
        return []
    try:
        with open(_JOURNAL_PATH) as f:
            data = json.load(f)
            if isinstance(data, list):
                return deduplicate_journal_entries(data)
            return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load conviction journal: %s", e)
        return []


def _save_journal(entries: list[dict]) -> None:
    """Write the conviction journal to disk atomically."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    deduped = deduplicate_journal_entries(entries)
    tmp_path = _JOURNAL_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(deduped, f, indent=2, default=str)
    os.replace(tmp_path, _JOURNAL_PATH)


# ---------------------------------------------------------------------------
# 1. Log a Conviction
# ---------------------------------------------------------------------------

async def log_conviction(
    ticker: str,
    direction: str,
    confidence: int,
    reasoning: str,
    signals: str = "",
    origin: str = "EXECUTED",
    playbook: Optional[str] = None,
    regime_posture: Optional[str] = None,
    session_id: Optional[str] = None,
    not_taken_reason: Optional[str] = None,
    target_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    entry_price_override: Optional[float] = None,
) -> dict[str, Any]:
    """
    Log a directional conviction for a ticker.

    Called by Sam or Vesper's reflection node when a thesis is formed.

    Args:
        ticker: Stock symbol (e.g. "NVDA")
        direction: "bullish", "bearish", or "neutral"
        confidence: 1-5 scale (1=speculative, 3=moderate, 5=slam dunk)
        reasoning: Key signals/logic driving the call
        signals: Comma-separated list of signal types (e.g. "RSI_oversold,EMA_bullish")
        origin: "EXECUTED", "REJECTED_BY_RISK_GATE", "REJECTED_BY_USER", "NOT_PROPOSED"
        playbook: Active strategy playbook (e.g. "momentum_squeeze", "0dte_flow")
        regime_posture: Market regime posture (e.g. "BULLISH", "DEFENSIVE")
        session_id: LangGraph execution session ID
        not_taken_reason: Explanation if proposal was rejected/blocked
        target_price: Expected profit target price
        stop_loss: Planned invalidation price
        entry_price_override: Optional pre-fetched live price or fill price

    Returns:
        Confirmation dict with entry details.
    """
    ticker = ticker.strip().upper()
    direction = direction.strip().lower()
    confidence = max(1, min(5, int(confidence)))

    if direction not in ("bullish", "bearish", "neutral"):
        return {"error": f"Invalid direction '{direction}'. Use bullish/bearish/neutral."}

    # Use price override if valid, else fetch live quote
    if entry_price_override is not None and float(entry_price_override) > 0:
        entry_price = float(entry_price_override)
    else:
        entry_price = await _fetch_price(ticker)

    if entry_price is None:
        return {"error": f"Could not fetch current price for {ticker}. Conviction NOT logged."}

    now = datetime.now(timezone.utc)
    short_uid = uuid.uuid4().hex[:6]
    entry_id = f"{ticker}:{now.strftime('%Y%m%d%H%M%S')}_{short_uid}"

    entry = {
        "id": entry_id,
        "ticker": ticker,
        "direction": direction,
        "confidence": confidence,
        "reasoning": reasoning,
        "signals": signals,
        "entry_price": round(entry_price, 2),
        "entry_date": now.isoformat(),
        "entry_ts": int(now.timestamp()),
        "origin": origin,
        "playbook": playbook,
        "regime_posture": regime_posture,
        "session_id": session_id,
        "not_taken_reason": not_taken_reason,
        "target_price": round(target_price, 2) if target_price is not None else None,
        "stop_loss": round(stop_loss, 2) if stop_loss is not None else None,
        # Resolution fields (filled later)
        "resolved": False,
        "resolutions": {},  # {horizon_days: {price, pct_move, result}}
    }

    journal = _load_journal()
    journal.append(entry)
    _save_journal(journal)

    # Sync to ChromaDB trade_memory for semantic recall
    try:
        from mcp_server.knowledge import ingest_trade_memory
        ingest_trade_memory(entry)
    except Exception as e:
        logger.debug("Trade memory auto-sync skipped: %s", e)

    logger.info(
        "📝 Conviction logged: %s %s on %s (confidence %d, origin=%s) @ $%.2f",
        direction, ticker, now.strftime("%Y-%m-%d"), confidence, origin, entry_price,
    )

    return {
        "status": "logged",
        "id": entry["id"],
        "ticker": ticker,
        "direction": direction,
        "confidence": confidence,
        "entry_price": entry_price,
        "origin": origin,
        "message": f"Conviction logged: {direction} {ticker} @ ${entry_price:.2f} (confidence {confidence}/5, origin={origin}). I'll check if I was right in 1/5/10 days.",
    }


# ---------------------------------------------------------------------------
# 2. Resolve Open Convictions
# ---------------------------------------------------------------------------

async def resolve_convictions() -> dict[str, Any]:
    """
    Check all unresolved convictions and score them.

    For each open conviction older than MIN_AGE_HOURS, fetches current price
    and scores at each horizon (1d, 5d, 10d) if enough time has elapsed.

    Results: WIN (≥1.5% move in predicted direction),
             LOSS (≥1.5% against), PUSH (neither).
    """
    journal = _load_journal()
    if not journal:
        return {"resolved": 0, "message": "No convictions to resolve."}

    now = datetime.now(timezone.utc)
    resolved_count = 0
    errors = 0

    for entry in journal:
        if entry.get("resolved"):
            continue

        entry_dt = datetime.fromisoformat(entry["entry_date"])
        age_hours = (now - entry_dt).total_seconds() / 3600

        if age_hours < MIN_AGE_HOURS:
            continue  # too fresh

        age_days = age_hours / 24
        entry_price = entry["entry_price"]
        direction = entry["direction"]
        resolutions = entry.get("resolutions", {})

        # Check each horizon that hasn't been resolved yet
        needs_price = False
        for horizon in RESOLUTION_HORIZONS:
            horizon_key = f"{horizon}d"
            if horizon_key in resolutions:
                continue  # already resolved at this horizon
            if age_days >= horizon:
                needs_price = True

        if not needs_price:
            continue

        # Fetch current price
        current_price = await _fetch_price(entry["ticker"])
        if current_price is None:
            errors += 1
            continue

        for horizon in RESOLUTION_HORIZONS:
            horizon_key = f"{horizon}d"
            if horizon_key in resolutions:
                continue
            if age_days < horizon:
                continue

            pct_move = ((current_price - entry_price) / entry_price) * 100

            # Score based on direction
            if direction == "bullish":
                if pct_move >= WIN_THRESHOLD_PCT:
                    result = "WIN"
                elif pct_move <= -LOSS_THRESHOLD_PCT:
                    result = "LOSS"
                else:
                    result = "PUSH"
            elif direction == "bearish":
                if pct_move <= -WIN_THRESHOLD_PCT:
                    result = "WIN"
                elif pct_move >= LOSS_THRESHOLD_PCT:
                    result = "LOSS"
                else:
                    result = "PUSH"
            else:  # neutral
                if abs(pct_move) <= WIN_THRESHOLD_PCT:
                    result = "WIN"  # neutral and stayed flat = correct
                else:
                    result = "LOSS"

            resolutions[horizon_key] = {
                "price": round(current_price, 2),
                "pct_move": round(pct_move, 2),
                "result": result,
                "resolved_at": now.isoformat(),
            }
            resolved_count += 1

        entry["resolutions"] = resolutions

        # Mark fully resolved if all horizons done
        if all(f"{h}d" in resolutions for h in RESOLUTION_HORIZONS):
            entry["resolved"] = True

    _save_journal(journal)

    # Sync resolved entries to ChromaDB trade_memory
    try:
        from mcp_server.knowledge import ingest_trade_memory
        for entry in journal:
            if entry.get("resolutions"):
                ingest_trade_memory(entry)
    except Exception as e:
        logger.debug("Trade memory auto-sync skipped: %s", e)

    return {
        "resolved": resolved_count,
        "errors": errors,
        "message": f"Resolved {resolved_count} horizon checks ({errors} errors).",
    }


def get_playbook_performance(playbook: str, days: int = 90) -> dict[str, Any]:
    """Calculate resolved performance and win rate for a specific playbook."""
    journal = _load_journal()
    if not journal:
        return {"playbook": playbook, "resolved": 0, "win_rate_pct": 0.0, "adjustment": 0.0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    wins = 0
    total = 0

    for entry in journal:
        if entry.get("playbook") != playbook:
            continue
        try:
            entry_dt = datetime.fromisoformat(entry["entry_date"])
            if entry_dt < cutoff:
                continue
        except Exception:
            pass

        resolutions = entry.get("resolutions", {})
        # Check best resolution
        for h in ["5d", "1d", "10d"]:
            if h in resolutions:
                res = resolutions[h].get("result")
                if res == "WIN":
                    wins += 1
                    total += 1
                elif res == "LOSS":
                    total += 1
                elif res == "PUSH":
                    total += 1
                break

    win_rate = (wins / total * 100.0) if total > 0 else 0.0

    # Calibration adjustment: +10% bonus if >65% win rate, -15% penalty if <35% win rate with >=3 samples
    if total >= 3:
        if win_rate >= 65.0:
            adj = 0.10
        elif win_rate <= 35.0:
            adj = -0.15
        else:
            adj = 0.0
    else:
        adj = 0.0

    return {
        "playbook": playbook,
        "resolved": total,
        "wins": wins,
        "win_rate_pct": round(win_rate, 1),
        "adjustment": adj,
    }


# ---------------------------------------------------------------------------
# 3. Track Record / Scorecard
# ---------------------------------------------------------------------------

async def get_track_record(
    ticker: str | None = None,
    days: int = 90,
) -> dict[str, Any]:
    """
    Get Sam's track record — batting average broken down by
    confidence tier, ticker, and direction.

    Auto-resolves open convictions first.

    Args:
        ticker: Optional filter by ticker
        days: How far back to look (default 90)

    Returns:
        Full scorecard with win/loss/push stats and calibration data.
    """
    # Resolve any open convictions first
    await resolve_convictions()

    journal = _load_journal()
    if not journal:
        return {
            "total_calls": 0,
            "message": "No convictions logged yet. I need to make some calls first!",
        }

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Filter entries
    filtered = []
    for entry in journal:
        entry_dt = datetime.fromisoformat(entry["entry_date"])
        if entry_dt < cutoff:
            continue
        if ticker and entry["ticker"] != ticker.strip().upper():
            continue
        filtered.append(entry)

    if not filtered:
        msg = f"No convictions in the last {days} days"
        if ticker:
            msg += f" for {ticker.upper()}"
        return {"total_calls": 0, "message": msg + "."}

    # Compute stats for a preferred horizon (5d, fallback to 1d, then 10d)
    def _best_resolution(entry: dict) -> dict | None:
        resolutions = entry.get("resolutions", {})
        for h in ["5d", "1d", "10d"]:
            if h in resolutions:
                return resolutions[h]
        return None

    # Overall stats
    wins = losses = pushes = 0
    by_confidence: dict[int, dict] = {}  # {confidence: {wins, losses, pushes}}
    by_direction: dict[str, dict] = {}
    by_ticker: dict[str, dict] = {}

    # Map result string → dict key (avoids "LOSS" → "losss" bug)
    _result_key = {"WIN": "wins", "LOSS": "losses", "PUSH": "pushes"}

    for entry in filtered:
        res = _best_resolution(entry)
        if res is None:
            continue  # not yet resolved

        result = res["result"]
        conf = entry["confidence"]
        direction = entry["direction"]
        tkr = entry["ticker"]
        rk = _result_key.get(result, "pushes")

        # Update overall
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1
        else:
            pushes += 1

        # By confidence
        if conf not in by_confidence:
            by_confidence[conf] = {"wins": 0, "losses": 0, "pushes": 0}
        by_confidence[conf][rk] += 1

        # By direction
        if direction not in by_direction:
            by_direction[direction] = {"wins": 0, "losses": 0, "pushes": 0}
        by_direction[direction][rk] += 1

        # By ticker
        if tkr not in by_ticker:
            by_ticker[tkr] = {"wins": 0, "losses": 0, "pushes": 0}
        by_ticker[tkr][rk] += 1

    total_resolved = wins + losses + pushes
    win_rate = (wins / total_resolved * 100) if total_resolved > 0 else 0

    # Calibration curve: for each confidence level, what's the actual hit rate?
    calibration = {}
    for conf in sorted(by_confidence.keys()):
        data = by_confidence[conf]
        total = data["wins"] + data["losses"] + data["pushes"]
        if total > 0:
            calibration[f"confidence_{conf}"] = {
                "calls": total,
                "wins": data["wins"],
                "win_rate_pct": round(data["wins"] / total * 100, 1),
                "expected_pct": conf * 20,  # 1→20%, 2→40%, 3→60%, 4→80%, 5→100%
            }

    # Recent calls (last 5)
    recent = []
    for entry in reversed(filtered[-5:]):
        res = _best_resolution(entry)
        recent.append({
            "ticker": entry["ticker"],
            "direction": entry["direction"],
            "confidence": entry["confidence"],
            "entry_price": entry["entry_price"],
            "date": entry["entry_date"][:10],
            "result": res["result"] if res else "PENDING",
            "pct_move": res.get("pct_move", None) if res else None,
        })

    return {
        "total_calls": len(filtered),
        "resolved": total_resolved,
        "pending": len(filtered) - total_resolved,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_pct": round(win_rate, 1),
        "calibration": calibration,
        "by_direction": by_direction,
        "by_ticker": by_ticker,
        "recent_calls": recent,
        "period_days": days,
        "message": _build_scorecard_summary(wins, losses, pushes, win_rate, calibration),
    }


def _build_scorecard_summary(
    wins: int, losses: int, pushes: int, win_rate: float, calibration: dict,
) -> str:
    """Build a human-readable scorecard summary for Sam to cite."""
    total = wins + losses + pushes
    if total == 0:
        return "No resolved calls yet — give me some time!"

    parts = [
        f"Track record: {wins}W / {losses}L / {pushes}P ({win_rate:.0f}% win rate) over {total} resolved calls.",
    ]

    # Highlight calibration insights
    for key, data in calibration.items():
        conf_level = key.split("_")[1]
        if data["calls"] >= 3:  # only cite with meaningful sample size
            parts.append(
                f"At confidence {conf_level}/5: {data['win_rate_pct']:.0f}% hit rate ({data['calls']} calls)."
            )

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Price helper
# ---------------------------------------------------------------------------

async def _fetch_price(ticker: str) -> float | None:
    """Fetch latest price for a ticker. Uses shared get_live_price helper."""
    try:
        from core.data import get_live_price
        return await get_live_price(ticker)
    except Exception as e:
        logger.warning("Price fetch failed for %s: %s", ticker, e)
        return None
