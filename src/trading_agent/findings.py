from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal[
    "ai-cli", "ai-local", "ai-key", "broker-key", "data-key",
    "mcp-entry", "gateway", "hermes-profile", "hermes-pool",
]
State = Literal["present", "absent", "unreadable", "blocked"]
Reusable = Literal["none", "reference-only", "copy-with-consent"]


@dataclass(frozen=True)
class Finding:
    """Presence record. Deliberately has no field that can hold a secret value."""

    id: str
    kind: Kind
    what: str
    where: str
    state: State
    names: tuple[str, ...]
    reusable: Reusable
