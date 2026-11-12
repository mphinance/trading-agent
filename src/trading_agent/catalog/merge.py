from __future__ import annotations

from typing import Any

from . import CatalogError

BANNER = "setup details not curated yet"
# Fields a curated overlay may supply. Everything else is owned by the generated layer.
OVERLAY_KEYS = frozenset(
    {"routes", "wizard", "region_codes", "region_notes", "overlay_reviewed_on", "sync"}
)


def _default_selectable(entry: dict[str, Any]) -> bool:
    kinds = entry.get("route_kinds", [])
    return ("remote" in kinds or "local" in kinds) and entry.get("status") != ["none"]


def merge_overlay(generated: list[dict], overlay: dict[str, dict]) -> list[dict]:
    known = {e["slug"] for e in generated}
    for slug in sorted(overlay):
        if slug not in known:
            raise CatalogError(f"overlay slug '{slug}' has no source file in the catalog")
    merged = []
    for gen in generated:
        entry = dict(gen)
        slug = entry["slug"]
        ov = overlay.get(slug)
        if ov is None:
            entry["routes"] = []
            entry["wizard"] = {"selectable": _default_selectable(entry), "banner": BANNER}
        else:
            bad = sorted(set(ov) - OVERLAY_KEYS)
            if bad:
                raise CatalogError(f"{slug}: overlay may not set generated field(s): {', '.join(bad)}")
            entry.update({k: v for k, v in ov.items() if k != "wizard"})
            entry.setdefault("routes", [])
            wizard = dict(ov.get("wizard") or {})
            wizard.setdefault("selectable", _default_selectable(entry))
            entry["wizard"] = wizard
        merged.append(entry)
    return sorted(merged, key=lambda e: e["slug"])
