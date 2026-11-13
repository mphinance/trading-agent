from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import date, datetime, timezone
from importlib import resources
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


def _packaged_overlay():
    """The overlay that ships inside the package (works from a wheel as well as a source checkout)."""
    return resources.files("trading_agent").joinpath("data").joinpath("overlay.json")


def default_overlay_text() -> str | None:
    """The packaged overlay's text, or None (with a one-line warning) when it is missing."""
    try:
        return _packaged_overlay().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        print("warning: the curated overlay file is missing, so no broker will be marked selectable.",
              file=sys.stderr)
        return None


def _route_kinds(server_type: str) -> list[str]:
    s = server_type.lower()
    kinds = [k for k in ("remote", "local") if k in s]
    if "n/a" in s:
        kinds.append("none")
    if "unknown" in s:
        kinds.append("unknown")
    return kinds or ["unknown"]


def _read_text(path: Path, rel: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Content-free on purpose: never echo file bytes.
        raise CatalogError(f"{rel}: unreadable or not UTF-8 text") from None


def _entry_from_file(path: Path, directory: str, today: date) -> dict:
    rel = f"{directory}/{path.name}"
    fm = parse_front_matter(_read_text(path, rel), rel)
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


_SUMMARY_COUNT = re.compile(r"<b>(\d+) brokers with no MCP route")


def _readme_stubs(catalog_root: Path, none_files: int, strict: bool) -> list[dict]:
    readme = next((p for p in (catalog_root / "readme.md", catalog_root / "README.md")
                   if p.is_file()), None)
    if readme is None:
        if strict:
            raise CatalogError("readme.md: not found, cannot extract no-route stubs")
        return []
    section = _NOROUTE_SECTION.search(_read_text(readme, readme.name))
    if not section:
        if strict:
            raise CatalogError(
                f"{readme.name}: no-route '<details>' section not found, cannot extract stubs")
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
    if strict:
        declared = int(_SUMMARY_COUNT.search(section.group(0)).group(1))  # regex matched above
        found = none_files + len(stubs)
        if declared != found:
            raise CatalogError(
                f"{readme.name}: summary says {declared} brokers with no MCP route, "
                f"found {none_files} file entries with status none + {len(stubs)} stubs = {found}")
    return stubs


def build_generated_layer(catalog_root: Path, today: date | None = None, *,
                          strict_readme: bool = True) -> list[dict]:
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
            # Policy: the catalog is a trusted checkout. Only regular files directly under
            # brokers/ and aggregators/ are read; a directory named *.md is skipped and a
            # symlink (even to a regular file) is rejected so nothing outside the root is read.
            if path.is_symlink():
                raise CatalogError(f"{directory}/{path.name}: symlinks are not allowed")
            if not path.is_file():
                continue
            entries.append(_entry_from_file(path, directory, today))
    none_files = sum(e["status"] == ["none"] for e in entries)
    entries.extend(_readme_stubs(catalog_root, none_files, strict_readme))
    seen: set[str] = set()
    for e in entries:
        if e["slug"] in seen:
            raise CatalogError(f"duplicate slug '{e['slug']}'")
        seen.add(e["slug"])
    return entries


def build_catalog(catalog_root: Path, overlay: dict[str, dict],
                  today: date | None = None, *, strict_readme: bool = True) -> list[dict]:
    merged = merge_overlay(
        build_generated_layer(catalog_root, today, strict_readme=strict_readme), overlay)
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


def _write_atomically(out: Path, text: str) -> None:
    """Temp file in the same directory, then os.replace; the temp file never outlives a failure."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="catalog-build")
    ap.add_argument("--catalog-root", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--overlay", type=Path, default=None,
                    help="overlay JSON keyed by slug (default: the overlay packaged with this tool)")
    args = ap.parse_args(argv)
    try:
        text = args.overlay.read_text(encoding="utf-8") if args.overlay else default_overlay_text()
        overlay = json.loads(text) if text else {}
        entries = build_catalog(args.catalog_root, overlay)
    except RecursionError:
        print("error: the input is nested too deeply", file=sys.stderr)
        return 1
    except (CatalogError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    doc = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "catalog_commit": _git_head(args.catalog_root),
        "entries": entries,
    }
    try:
        _write_atomically(args.out, json.dumps(doc, indent=2) + "\n")
    except OSError as exc:
        print(f"error: could not write the output file ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0
