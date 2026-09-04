#!/usr/bin/env python3
"""
scripts/export_public.py — build the public export from an explicit manifest.

    python scripts/export_public.py                  # dry run: list, validate, print
    python scripts/export_public.py --out DIR        # stage the export into DIR

The destination is `mphinance/momentum-mcp`, which already exists, is public and
carries 26 stars and 8 forks — so this UPDATES a repo with standing rather than
seeding a new one. Staging writes to a directory; publishing is a human running
git in it. This script never runs git, never pushes, and never touches a remote.

Two properties make it safe to point at a public repo:

- **The manifest is explicit — no globs over `core/`.** A new module in `core/`
  is private until someone adds its name here, and
  `tests/test_public_export.py` fails the build if the staged set drifts from
  its reviewed baseline. That is the whole safety model: publishing is opt-in,
  per file, reviewed.
- **The closure is verified, not assumed.** `mcp_server/`'s transitive imports
  reach `vesper/` and `trading_mcp/` zero times, including deferred and
  function-level imports, pinned independently in
  `tests/test_import_boundaries.py`.

`core/traderdaddy.py` is in the manifest only because it was repointed at the
customer API (`/api/v1/*`, API key). While it spoke to `/api/agent/*` — the
internal superuser namespace — it was unshippable, and the same would be true of
anything else that grows a dependency on a master credential.
"""

from __future__ import annotations

import argparse
import shutil
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


def stage(out_dir: Path) -> dict[str, int]:
    """Copy the manifest into `out_dir`, preserving `core/` and `mcp_server/`.

    Refuses a destination that already holds files the manifest does not name.
    Overwriting an arbitrary directory — someone's home, a checkout with local
    work in it — is the one irreversible thing this script could do, so it
    declines rather than guessing.
    """
    files = get_manifest_files()
    expected = {f"{f.parent.name}/{f.name}" for f in files}

    if out_dir.exists():
        strays = {
            f"{p.parent.name}/{p.name}"
            for p in out_dir.rglob("*.py")
            if p.parent.name in {"core", "mcp_server"}
        } - expected
        if strays:
            raise RuntimeError(
                f"{out_dir} contains Python files the manifest does not name: "
                f"{sorted(strays)}. Refusing to write into it — remove them, or "
                "stage into a clean directory and diff the two."
            )

    for src in files:
        dest = out_dir / src.parent.name / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    total_lines = sum(len(f.read_text(encoding="utf-8").splitlines()) for f in files)
    print(f"Staged {len(files)} files ({total_lines} lines) -> {out_dir}")
    print("Nothing was committed or pushed. Review the diff, then publish by hand.")
    return {"file_count": len(files), "total_lines": total_lines}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="stage the export into this directory (default: dry run only)",
    )
    args = parser.parse_args()
    try:
        if args.out is None:
            dry_run()
        else:
            stage(args.out)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
