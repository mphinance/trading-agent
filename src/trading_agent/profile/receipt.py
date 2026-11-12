"""The setup receipt: names and locations only, never a value.

Saved per profile at ``<config_home>/trading-agent/receipts/<profile>.json`` (file 0600, dir 0700,
atomic write) after every write step, so a crash leaves enough to undo exactly what this run created.
"""
from __future__ import annotations

import json
import os
import secrets as _stdlib_secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from trading_agent.platform import Platform
from trading_agent.profile.hermes_cli import validate_profile_name


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
    run_id: str = ""                   # random id also written to <profile>/.trading-agent-run (ownership proof)

    @staticmethod
    def receipts_dir(platform: Platform) -> Path:
        return Path(platform.config_home()) / "trading-agent" / "receipts"

    @staticmethod
    def path(platform: Platform, profile_name: str) -> Path:
        validate_profile_name(profile_name)  # the name becomes a file name
        return Receipt.receipts_dir(platform) / f"{profile_name}.json"

    @staticmethod
    def list_receipts(platform: Platform) -> list[str]:
        """Profile names that have a saved receipt."""
        try:
            return sorted(p.stem for p in Receipt.receipts_dir(platform).glob("*.json") if not p.name.startswith("."))
        except OSError:
            return []

    def save(self, platform: Platform) -> None:
        target = self.path(platform, self.profile_name)
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
    def load(cls, platform: Platform, profile_name: str) -> "Receipt | None":
        target = cls.path(platform, profile_name)
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
        run_id = d.get("run_id", "")
        if not isinstance(run_id, str):
            raise ReceiptError("receipt field run_id is malformed")
        return cls(profile_name=d["profile_name"], ai_choice=d.get("ai_choice"), broker=d.get("broker"),
                   run_id=run_id, **lists)

    def remove_file(self, platform: Platform) -> None:
        """Delete this profile's saved receipt, but only if the saved one carries the same run id."""
        target = self.path(platform, self.profile_name)
        try:
            saved = Receipt.load(platform, self.profile_name)
        except ReceiptError:
            return  # unreadable: not provably ours, leave it for a human
        if saved is None or saved.run_id != self.run_id:
            return
        try:
            target.unlink()
        except FileNotFoundError:
            pass
