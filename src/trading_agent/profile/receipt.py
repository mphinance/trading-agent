"""The setup receipt: names and locations only, never a value.

Saved at ``<config_home>/trading-agent/receipt.json`` (mode 0600, atomic write) after every
write step, so a crash leaves enough to undo exactly what this run created.
"""
from __future__ import annotations

import json
import os
import secrets as _stdlib_secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from trading_agent.platform import Platform


class ReceiptError(Exception):
    """The receipt file is unreadable or malformed. Value-free."""


@dataclass
class Receipt:
    profile_name: str
    created_paths: list[str] = field(default_factory=list)
    env_names_written: list[str] = field(default_factory=list)
    steps_done: list[str] = field(default_factory=list)
    ai_choice: dict | None = None      # {"kind", "ref"}: ref is a CLI binary name or a base URL, never a key
    broker: dict | None = None         # {"slug", "route_id"}
    skills: list[str] = field(default_factory=list)

    @staticmethod
    def path(platform: Platform) -> Path:
        return Path(platform.config_home()) / "trading-agent" / "receipt.json"

    def save(self, platform: Platform) -> None:
        target = self.path(platform)
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = directory / f".{target.name}.{_stdlib_secrets.token_hex(8)}.tmp"
        data = json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
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
    def load(cls, platform: Platform) -> "Receipt | None":
        target = cls.path(platform)
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            raise ReceiptError("receipt file unreadable") from None
        try:
            d = json.loads(raw)
        except ValueError:
            raise ReceiptError("receipt file is not valid JSON") from None
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, d: object) -> "Receipt":
        if not isinstance(d, dict) or not isinstance(d.get("profile_name"), str):
            raise ReceiptError("receipt has no profile_name")
        lists = {}
        for key in ("created_paths", "env_names_written", "steps_done", "skills"):
            v = d.get(key, [])
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ReceiptError(f"receipt field {key} is malformed")
            lists[key] = list(v)
        for key in ("ai_choice", "broker"):
            v = d.get(key)
            if v is not None and not (isinstance(v, dict) and all(isinstance(x, str) for x in v.values())):
                raise ReceiptError(f"receipt field {key} is malformed")
        return cls(profile_name=d["profile_name"], ai_choice=d.get("ai_choice"), broker=d.get("broker"), **lists)

    def remove_file(self, platform: Platform) -> None:
        """Delete the saved receipt, but only if it belongs to THIS profile name."""
        target = self.path(platform)
        try:
            saved = Receipt.load(platform)
        except ReceiptError:
            saved = None
        if saved is not None and saved.profile_name != self.profile_name:
            return
        try:
            target.unlink()
        except FileNotFoundError:
            pass
