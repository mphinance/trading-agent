"""Compatibility re-export: halt.py's real implementation moved to core/halt.py
in M0-03 (four pure state-I/O modules -> core/, confirmed stdlib-only).

This shim exists for exactly one reason: vesper/execution_guard.py contains
`from vesper.halt import is_halted` (guarded halt checks in preview()/place()),
and execution_guard.py is never to be edited -- see repo policy (it is the one
module allowed to move money, and an agent editing its import lines is still
an agent editing the order path). Every other caller in this repo was
repointed to `core.halt` directly; this file keeps that one import path
resolving to the exact same objects.

These are the same function objects as core.halt's (not copies), so their
__globals__ still point at core.halt's module namespace -- monkeypatching
core.halt._DATA_DIR / core.halt._HALT_STATE_PATH (as tests/conftest.py now
does) governs their behavior identically to before the move. Patching
vesper.halt._DATA_DIR instead would NOT work, since that would only rebind a
name in this shim module's namespace, not core.halt's.
"""

from __future__ import annotations

from core.halt import (
    get_halt_status,
    halt,
    is_halted,
    resume,
)

__all__ = ["get_halt_status", "halt", "is_halted", "resume"]
