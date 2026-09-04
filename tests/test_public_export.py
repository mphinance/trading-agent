"""Safety gate for public export: assert the manifest is completely hermetic."""

from __future__ import annotations

import ast
from pathlib import Path
import pytest

from scripts.export_public import (
    EXCLUDE,
    PUBLIC_CORE_MODULES,
    get_manifest_files,
    REPO_ROOT,
)
from tests.test_import_boundaries import _refs_to_targets_in_file, _vesper_refs_in_file


def _get_all_imports_in_file(path: Path) -> set[str]:
    """Extract all imported module/symbol names from path's AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module)
                for alias in node.names:
                    imported.add(f"{node.module}.{alias.name}")
    return imported


# The exact set of files the public export would publish. This is a review
# surface, not bookkeeping: every name here is a file that would become world
# readable, so adding one is a decision someone made on purpose.
EXPECTED_PUBLIC_FILES = {
    "core/__init__.py",
    "core/cache.py",
    "core/charts.py",
    "core/conviction.py",
    "core/data.py",
    "core/edgar.py",
    "core/knowledge.py",
    "core/macro_regime.py",
    "core/market_top.py",
    "core/options.py",
    "core/options_greeks.py",
    "core/risk.py",
    "core/schema.py",
    "core/screener.py",
    "core/technicals.py",
    "core/traderdaddy.py",
    "core/vcp_screener.py",
    "mcp_server/__init__.py",
    "mcp_server/alpha_cards.py",
    "mcp_server/backtest.py",
    "mcp_server/breadth.py",
    "mcp_server/bubble.py",
    "mcp_server/canslim_screener.py",
    "mcp_server/earnings_analyzer.py",
    "mcp_server/edgar_tools.py",
    "mcp_server/environment.py",
    "mcp_server/exposure.py",
    "mcp_server/ftd_detector.py",
    "mcp_server/fundamentals.py",
    "mcp_server/garch.py",
    "mcp_server/macro.py",
    "mcp_server/memory.py",
    "mcp_server/news.py",
    "mcp_server/pair_trade.py",
    "mcp_server/pead_screener.py",
    "mcp_server/position_sizer.py",
    "mcp_server/registry.py",
    "mcp_server/scenario.py",
    "mcp_server/server.py",
    "mcp_server/themes.py",
    "mcp_server/tv_analysis.py",
    "mcp_server/upsell.py",
    "mcp_server/uptrend.py",
    "mcp_server/warmer.py",
    "mcp_server/watchlist.py",
}


class TestExportManifestInventory:
    def test_manifest_files_exist(self):
        files = get_manifest_files()
        for f in files:
            assert f.is_file(), f"Manifest file does not exist: {f}"

    def test_manifest_contents_are_deliberate(self):
        """A tripwire on WHAT is in the export, not how many things are.

        This assertion used to be `len(files) == 44`, which fired correctly the
        first time a module was added — and said only that a number had moved.
        Comparing names means the failure names the file, and the reviewer can
        see at a glance whether it is something that should be published.

        Adding a module here is the deliberate act. Read it and decide, then add
        the name.
        """
        actual = {f"{f.parent.name}/{f.name}" for f in get_manifest_files()}
        unexpected = actual - EXPECTED_PUBLIC_FILES
        missing = EXPECTED_PUBLIC_FILES - actual
        assert not unexpected, (
            "new file(s) would be published that this baseline has never seen: "
            f"{sorted(unexpected)}. If they belong in the public repo, add them "
            "to EXPECTED_PUBLIC_FILES; if they carry anything private, they do "
            "not belong in the manifest at all."
        )
        assert not missing, (
            f"manifest no longer exports: {sorted(missing)} — was that intended?"
        )

    def test_constellation_excluded(self):
        files = get_manifest_files()
        filenames = [f.name for f in files]
        assert "constellation.py" not in filenames

    def test_core_modules_completeness(self):
        expected_core = {
            "cache", "charts", "conviction", "data", "edgar", "knowledge",
            "macro_regime", "market_top", "options", "options_greeks", "risk",
            "schema", "screener", "technicals", "traderdaddy", "vcp_screener",
        }
        assert set(PUBLIC_CORE_MODULES) == expected_core


class TestPublicExportBoundaries:
    """Strict boundary checks: manifest transitive closure must never escape."""

    def test_manifest_never_imports_vesper(self):
        files = get_manifest_files()
        violations: dict[str, set[str]] = {}

        for f in files:
            refs = _vesper_refs_in_file(f)
            if refs:
                violations[str(f.relative_to(REPO_ROOT))] = refs

        assert not violations, (
            f"Public manifest files must NEVER import vesper! Violations: {violations}"
        )

    def test_manifest_never_imports_trading_mcp(self):
        files = get_manifest_files()
        violations: dict[str, set[str]] = {}

        for f in files:
            refs = _refs_to_targets_in_file(f, ("trading_mcp",))
            if refs:
                violations[str(f.relative_to(REPO_ROOT))] = refs

        assert not violations, (
            f"Public manifest files must NEVER import trading_mcp! Violations: {violations}"
        )

    def test_manifest_never_imports_unapproved_core_modules(self):
        files = get_manifest_files()
        unapproved_violations: dict[str, set[str]] = {}

        allowed_core_set = set(PUBLIC_CORE_MODULES)

        for f in files:
            all_imports = _get_all_imports_in_file(f)
            bad_imports: set[str] = set()

            for imp in all_imports:
                if imp == "core" or imp.startswith("core."):
                    parts = imp.split(".")
                    if len(parts) > 1:
                        submod = parts[1]
                        if submod not in allowed_core_set and submod != "__init__":
                            bad_imports.add(imp)

            if bad_imports:
                unapproved_violations[str(f.relative_to(REPO_ROOT))] = bad_imports

        assert not unapproved_violations, (
            f"Public manifest files must NEVER import unapproved core modules (e.g. broker clients, secret hygiene)! "
            f"Violations: {unapproved_violations}"
        )

    def test_manifest_never_imports_excluded_files(self):
        files = get_manifest_files()
        violations: dict[str, set[str]] = {}

        for f in files:
            refs = _refs_to_targets_in_file(f, ("mcp_server.constellation",))
            if refs:
                violations[str(f.relative_to(REPO_ROOT))] = refs

        assert not violations, (
            f"Public manifest files must not import excluded modules: {violations}"
        )
