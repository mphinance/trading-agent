"""Remote Kill Switch & Emergency Freeze Engine for Vesper.

Provides instant runtime halting independent of environment variables or process restarts.
Controlled via CLI (`vesper halt` / `vesper resume`), bot commands (`/halt`), and execution guards.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_HALT_STATE_PATH = _DATA_DIR / "halt_state.json"


def _load_state() -> Dict[str, Any]:
    if not _HALT_STATE_PATH.exists():
        return {"is_halted": False}
    try:
        with open(_HALT_STATE_PATH) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read halt state file: {e}")
        return {"is_halted": False}


def _save_state(state: Dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _HALT_STATE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, _HALT_STATE_PATH)


def is_halted() -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Check whether Vesper is currently halted under an emergency freeze."""
    state = _load_state()
    if state.get("is_halted", False):
        return True, state
    return False, None


def halt(reason: str = "Manual emergency halt triggered", source: str = "cli") -> Dict[str, Any]:
    """Trigger an immediate emergency halt, freezing all execution paths."""
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "is_halted": True,
        "halted_at": now,
        "halted_by": source,
        "reason": reason,
    }
    _save_state(state)
    logger.critical(f"🛑 [EMERGENCY HALT] Vesper execution frozen by {source}: {reason}")
    return {
        "status": "HALTED",
        "message": f"Emergency halt active: {reason} (triggered by {source} at {now})",
        "state": state,
    }


def resume(source: str = "cli") -> Dict[str, Any]:
    """Clear emergency halt and restore normal system readiness."""
    prev_state = _load_state()
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "is_halted": False,
        "resumed_at": now,
        "resumed_by": source,
        "previous_halt": prev_state if prev_state.get("is_halted") else None,
    }
    _save_state(state)
    logger.info(f"✅ [SYSTEM RESUMED] Vesper emergency halt cleared by {source} at {now}")
    return {
        "status": "ACTIVE",
        "message": f"Emergency halt cleared by {source} at {now}. Normal trading enabled.",
        "state": state,
    }


def get_halt_status() -> Dict[str, Any]:
    """Return current halt status and metadata."""
    state = _load_state()
    return {
        "is_halted": bool(state.get("is_halted", False)),
        "details": state,
    }
