"""The catalog snapshot shipped inside the package (``data/catalog.json``), built by ``trading-agent catalog build``."""
from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path

NOTICE = "The broker catalog could not be read, so no brokers are offered. Reinstall the tool to restore it."


def load_catalog(path: Path | None = None) -> list[dict]:
    """The snapshot's entries. Unreadable or malformed: an empty list and one plain line on stderr."""
    try:
        source = path if path is not None else resources.files("trading_agent").joinpath("data").joinpath("catalog.json")
        doc = json.loads(source.read_text(encoding="utf-8"))
        entries = doc["entries"]
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise ValueError
        return entries
    except (OSError, ValueError, KeyError, TypeError):
        print(NOTICE, file=sys.stderr)
        return []
