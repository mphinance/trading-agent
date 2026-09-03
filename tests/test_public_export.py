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


class TestExportManifestInventory:
    def test_manifest_files_exist(self):
        files = get_manifest_files()
        assert len(files) == 44
        for f in files:
            assert f.is_file(), f"Manifest file does not exist: {f}"

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
