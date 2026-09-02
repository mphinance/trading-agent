"""Portfolio-Level Drawdown Circuit Breaker.

Tracks a persisted high-water-mark NLV across runs and trips core.halt's
existing emergency halt when current NLV falls VESPER_CIRCUIT_BREAKER_PCT
(default 15%) below that peak. This is a portfolio-level backstop, distinct
from execution_guard's per-order notional/quantity caps -- a series of
individually-compliant trades can still bleed an account dry, and nothing
before this watched for that.

State lives in its own file (same atomic-write pattern as core/halt.py),
not inside halt_state.json -- halt.py's state is "are we halted and why,"
this module's state is "what's the peak NLV we're measuring drawdown from,"
and conflating the two would make halt.py's simple boolean model do double
duty for a different concern.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_STATE_PATH = _DATA_DIR / "circuit_breaker_state.json"

DEFAULT_DRAWDOWN_PCT = 0.15


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _load_state() -> Dict[str, Any]:
    if not _STATE_PATH.exists():
        return {}
    try:
        with open(_STATE_PATH) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read circuit breaker state file: {e}")
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _STATE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, _STATE_PATH)


def get_peak_nlv() -> float:
    """Read the current tracked peak without mutating it. Returns 0.0 if never recorded."""
    return float(_load_state().get("peak_nlv", 0.0))


def check_portfolio_drawdown(
    current_nlv: float,
    threshold_pct: float = DEFAULT_DRAWDOWN_PCT,
) -> Dict[str, Any]:
    """Update the tracked peak NLV and trip halt() if drawdown from peak >= threshold.

    Idempotent and safe to call on every risk_gate_node pass:
    - Never re-halts if already halted (avoids stomping the existing halt
      reason/timestamp on every subsequent call, and avoids a halt-storm).
    - Starts a FRESH peak the first time it's called after a resume. Without
      this, resuming after a drawdown-triggered halt would immediately see
      the same >=threshold drawdown from the stale old peak and re-halt on
      the very next check, making /resume useless for this specific halt
      cause. The tradeoff: a resumed session gets a full fresh runway before
      the breaker can trip again, rather than "keep falling from the old
      high," which is the intended behavior for a human who has just decided
      to keep trading after reviewing what happened.
    - current_nlv <= 0 is treated as "unknown," not "total loss" -- skip
      rather than let a bad/zero read from a broker hiccup trip the breaker
      or corrupt the peak.
    """
    from core.halt import halt, is_halted

    if current_nlv is None or current_nlv <= 0:
        return {"skipped": True, "reason": "current_nlv unavailable or non-positive"}

    state = _load_state()
    halted, _ = is_halted()

    if not halted and state.get("breaker_tripped_at"):
        # A resume happened since we last tripped -- start measuring drawdown
        # fresh from here rather than keep comparing against the old peak.
        state = {"peak_nlv": current_nlv, "peak_at": datetime.now(timezone.utc).isoformat()}

    peak_nlv = float(state.get("peak_nlv", 0.0))
    if current_nlv > peak_nlv:
        peak_nlv = current_nlv
        state["peak_nlv"] = peak_nlv
        state["peak_at"] = datetime.now(timezone.utc).isoformat()

    drawdown_pct = ((peak_nlv - current_nlv) / peak_nlv) if peak_nlv > 0 else 0.0
    tripped_now = False

    if not halted and drawdown_pct >= threshold_pct:
        halt(
            reason=(
                f"Portfolio circuit breaker: {drawdown_pct:.1%} drawdown from peak NLV "
                f"${peak_nlv:,.2f} (current ${current_nlv:,.2f}), threshold {threshold_pct:.0%}"
            ),
            source="circuit_breaker",
        )
        state["breaker_tripped_at"] = datetime.now(timezone.utc).isoformat()
        tripped_now = True

    _save_state(state)

    return {
        "peak_nlv": peak_nlv,
        "current_nlv": current_nlv,
        "drawdown_pct": drawdown_pct,
        "threshold_pct": threshold_pct,
        "tripped_now": tripped_now,
    }


def get_configured_threshold() -> float:
    return _env_float("VESPER_CIRCUIT_BREAKER_PCT", DEFAULT_DRAWDOWN_PCT)
