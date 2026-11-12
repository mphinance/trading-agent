from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path

from . import CatalogError
from .frontmatter import parse_front_matter
from .merge import merge_overlay
from .schema import validate_entry

STATUS_TOKENS = ("official", "community", "aggregator-only", "none")
TRADING_VALUES = ("true", "false", "draft-only")
REQUIRED_KEYS = ("name", "status", "trading", "server_type", "source_url", "last_verified")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NOROUTE_SECTION = re.compile(
    r"<details>\s*<summary><b>\d+ brokers with no MCP route[\s\S]*?</details>"
)
DEFAULT_OVERLAY = Path(__file__).resolve().parents[3] / "catalog" / "overlay.json"


def _route_kinds(server_type: str) -> list[str]:
    s = server_type.lower()
    kinds = [k for k in ("remote", "local") if k in s]
    if "n/a" in s:
        kinds.append("none")
    if "unknown" in s:
        kinds.append("unknown")
    return kinds or ["unknown"]


def _entry_from_file(path: Path, directory: str, today: date) -> dict:
    rel = f"{directory}/{path.name}"
    fm = parse_front_matter(path.read_text(encoding="utf-8"), rel)
    for key in REQUIRED_KEYS:
        if not fm.get(key):
            raise CatalogError(f"{rel}: missing '{key}'")
    status = [t.strip() for t in fm["status"].split("+")]
    for tok in status:
        if tok not in STATUS_TOKENS:
            raise CatalogError(
                f"{rel}: status token '{tok}' not in {' | '.join(STATUS_TOKENS)}")
    if fm["trading"] not in TRADING_VALUES:
        raise CatalogError(
            f"{rel}: trading '{fm['trading']}' not in {' | '.join(TRADING_VALUES)}")
    lv = fm["last_verified"]
    try:
        verified = date.fromisoformat(lv) if _DATE.match(lv) else None
    except ValueError:
        verified = None
    if verified is None:
        raise CatalogError(f"{rel}: last_verified '{lv}' is not YYYY-MM-DD")
    if verified > today:
        raise CatalogError(f"{rel}: last_verified {lv} is in the future")
    entry = {
        "slug": path.stem,
        "name": fm["name"],
        "directory": directory,
        "page_path": rel,
        "source": "front-matter",
        "status": status,
        "trading": fm["trading"],
        "server_type_raw": fm["server_type"],
        "route_kinds": _route_kinds(fm["server_type"]),
        "source_url": fm["source_url"],
        "last_verified": lv,
    }
    if fm.get("region"):
        entry["region_raw"] = fm["region"]
    return entry


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _readme_stubs(catalog_root: Path) -> list[dict]:
    readme = next((p for p in (catalog_root / "readme.md", catalog_root / "README.md")
                   if p.is_file()), None)
    if readme is None:
        return []
    section = _NOROUTE_SECTION.search(readme.read_text(encoding="utf-8"))
    if not section:
        return []
    stubs = []
    for line in section.group(0).split("\n"):
        # Same rule as check-readme-stats.mjs: table rows with no link to an entry file.
        if not line.startswith("| ") or line.startswith("| Broker") or line.startswith("|---"):
            continue
        if re.search(r"\((brokers|aggregators)/", line):
            continue
        name = line.split("|")[1].replace("*", "").strip()
        stubs.append({
            "slug": _slugify(name),
            "name": name,
            "directory": "readme-only",
            "page_path": "",
            "source": "readme-only",
            "status": ["none"],
            "trading": "false",
            "server_type_raw": "n/a",
            "route_kinds": ["none"],
        })
    return stubs


def build_generated_layer(catalog_root: Path, today: date | None = None) -> list[dict]:
    today = today or date.today()
    entries: list[dict] = []
    for directory in ("brokers", "aggregators"):
        folder = catalog_root / directory
        if not folder.is_dir():
            raise CatalogError(f"{directory}/: directory not found under catalog root")
        for path in sorted(folder.glob("*.md")):
            # Same skips as check-readme-stats.mjs.
            if path.name == "readme.md" or path.name.startswith("_"):
                continue
            entries.append(_entry_from_file(path, directory, today))
    entries.extend(_readme_stubs(catalog_root))
    seen: set[str] = set()
    for e in entries:
        if e["slug"] in seen:
            raise CatalogError(f"duplicate slug '{e['slug']}'")
        seen.add(e["slug"])
    return entries


def build_catalog(catalog_root: Path, overlay: dict[str, dict],
                  today: date | None = None) -> list[dict]:
    merged = merge_overlay(build_generated_layer(catalog_root, today), overlay)
    for entry in merged:
        validate_entry(entry)
    return merged


def _git_head(root: Path) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                           text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and sha else None


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="catalog-build")
    ap.add_argument("--catalog-root", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--overlay", type=Path, default=None,
                    help="overlay JSON keyed by slug (default: catalog/overlay.json of this repo)")
    args = ap.parse_args(argv)
    try:
        overlay_path = args.overlay or (DEFAULT_OVERLAY if DEFAULT_OVERLAY.is_file() else None)
        overlay = json.loads(overlay_path.read_text(encoding="utf-8")) if overlay_path else {}
        entries = build_catalog(args.catalog_root, overlay)
    except (CatalogError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    doc = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "catalog_commit": _git_head(args.catalog_root),
        "entries": entries,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return 0
