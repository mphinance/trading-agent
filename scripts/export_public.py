#!/usr/bin/env python3
"""
scripts/export_public.py — Build and validate the public export manifest (dry-run only).

Validates all files in the quant-mcp public manifest and prints the inventory.
Does NOT copy, write, or push anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Public Export Manifest (exact, no globs, no guessing)
# ---------------------------------------------------------------------------

PUBLIC_MCP_SERVER = ["mcp_server/"]          # except constellation.py, see below
PUBLIC_CORE_MODULES = [
    "cache", "charts", "conviction", "data", "edgar", "knowledge",
    "macro_regime", "market_top", "options", "options_greeks", "risk",
    "schema", "screener", "technicals", "traderdaddy", "vcp_screener",
]
EXCLUDE = ["mcp_server/constellation.py"]    # dead code: imported by nothing


def get_manifest_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Resolve and validate the exact list of files in the public export manifest."""
    files: list[Path] = []

    # 1. MCP Server files (all in mcp_server/ except EXCLUDE)
    mcp_dir = repo_root / "mcp_server"
    if not mcp_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {mcp_dir}")

    exclude_set = {repo_root / e for e in EXCLUDE}

    for p in sorted(mcp_dir.iterdir()):
        if p.is_file() and p.suffix == ".py":
            if p not in exclude_set:
                files.append(p)

    # 2. Core modules + core/__init__.py
    core_init = repo_root / "core" / "__init__.py"
    if not core_init.is_file():
        raise FileNotFoundError(f"Missing {core_init}")
    files.append(core_init)

    for mod_name in PUBLIC_CORE_MODULES:
        mod_file = repo_root / "core" / f"{mod_name}.py"
        if not mod_file.is_file():
            raise FileNotFoundError(f"Manifest module not found: {mod_file}")
        files.append(mod_file)

    return sorted(files)


def dry_run() -> dict[str, int]:
    """Perform dry run: validate files exist, compute statistics, print report."""
    print("=" * 70)
    print("QUANT-MCP PUBLIC EXPORT MANIFEST (DRY RUN)")
    print("=" * 70)

    files = get_manifest_files()
    total_lines = 0

    mcp_files = [f for f in files if "mcp_server" in f.parts]
    core_files = [f for f in files if "core" in f.parts]

    print(f"\nMCP Server Files ({len(mcp_files)} files):")
    for f in mcp_files:
        lines = len(f.read_text(encoding="utf-8").splitlines())
        total_lines += lines
        rel = f.relative_to(REPO_ROOT)
        print(f"  {str(rel):<40} ({lines:>5} lines)")

    print(f"\nCore Analytics Modules ({len(core_files)} files):")
    for f in core_files:
        lines = len(f.read_text(encoding="utf-8").splitlines())
        total_lines += lines
        rel = f.relative_to(REPO_ROOT)
        print(f"  {str(rel):<40} ({lines:>5} lines)")

    print("-" * 70)
    print(f"Excluded: {', '.join(EXCLUDE)}")
    print(f"Total manifest files: {len(files)}")
    print(f"Total lines of code:  {total_lines}")
    print("=" * 70)
    print("DRY RUN COMPLETE — No files were copied, modified, or exported.")
    print("=" * 70)

    return {"file_count": len(files), "total_lines": total_lines}


if __name__ == "__main__":
    try:
        dry_run()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
