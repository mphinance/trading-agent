"""Wizard state: where the user is and which decisions they made. Never a value.

Saved as JSON at ``<config_home>/trading-agent/state.json`` (file 0600, directory 0700, atomic write)
after every change. It holds finding ids and decision keys only. ``set`` refuses anything that does
not look like a short decision token, which is a second net behind the flow's own discipline.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _stdlib_secrets
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from trading_agent.platform import Platform

_KEY_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")
_VALUE_MAX = 512


class State(Enum):
    START = "START"
    PRECHECK = "PRECHECK"
    SCAN = "SCAN"
    AI_ROUTE = "AI_ROUTE"
    AI_VERIFY = "AI_VERIFY"
    BROKER_PICK = "BROKER_PICK"
    BROKER_INSTALL = "BROKER_INSTALL"
    BROKER_VERIFY = "BROKER_VERIFY"
    SKILLS = "SKILLS"
    DATA_KEYS = "DATA_KEYS"
    REVIEW = "REVIEW"
    WRITE = "WRITE"
    VERIFY = "VERIFY"
    DONE = "DONE"


def state_path(platform: Platform) -> Path:
    return Path(platform.config_home()) / "trading-agent" / "state.json"


@dataclass
class WizardState:
    current: State = State.START
    decisions: dict[str, str] = field(default_factory=dict)

    def set(self, key: str, value: str) -> None:
        """Record a decision. Raises a value-free ValueError for anything that is not a short plain token."""
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError("invalid decision key")
        if (not isinstance(value, str) or len(value) > _VALUE_MAX
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError(f"invalid decision value for {key}")
        self.decisions[key] = value

    def forget(self, key: str) -> None:
        self.decisions.pop(key, None)

    def save(self, platform: Platform) -> None:
        target = state_path(platform)
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = directory / f".{target.name}.{_stdlib_secrets.token_hex(8)}.tmp"
        data = json.dumps({"current": self.current.value, "decisions": self.decisions}, indent=2, sort_keys=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, platform: Platform) -> "WizardState":
        """The saved state, or a fresh one if there is none or it is unreadable. Never raises, never echoes."""
        try:
            raw = state_path(platform).read_text(encoding="utf-8")
            doc = json.loads(raw)
            current = State(doc["current"])
            decisions = doc["decisions"]
            if not isinstance(decisions, dict):
                raise ValueError
            ws = cls(current)
            for k, v in decisions.items():
                ws.set(k, v)
            return ws
        except (OSError, ValueError, KeyError, TypeError):
            return cls()
