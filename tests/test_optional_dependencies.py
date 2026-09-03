"""Tests asserting heavy dependencies (chromadb, matplotlib) are properly guarded."""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _has_bare_module_level_import(filepath: Path, target_module: str) -> bool:
    """Return True if filepath contains a bare (not in Try) module-level import of target_module."""
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_module or alias.name.startswith(f"{target_module}."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == target_module or mod.startswith(f"{target_module}."):
                return True
    return False


class TestAstGuards:
    def test_knowledge_has_no_bare_chromadb_import(self):
        knowledge_path = PROJECT_ROOT / "core" / "knowledge.py"
        assert not _has_bare_module_level_import(knowledge_path, "chromadb"), (
            "core/knowledge.py contains a bare module-level chromadb import"
        )

    def test_backtest_has_no_bare_matplotlib_import(self):
        backtest_path = PROJECT_ROOT / "mcp_server" / "backtest.py"
        assert not _has_bare_module_level_import(backtest_path, "matplotlib"), (
            "mcp_server/backtest.py contains a bare module-level matplotlib import"
        )


class TestChromadbOptional:
    def test_knowledge_imports_without_chromadb(self, monkeypatch):
        import core.knowledge as knowledge_mod

        # Block chromadb import
        monkeypatch.setitem(sys.modules, "chromadb", None)
        try:
            reloaded = importlib.reload(knowledge_mod)
            assert reloaded._CHROMADB_AVAILABLE is False
            assert reloaded.chromadb is None
        finally:
            # Restore
            monkeypatch.undo()
            importlib.reload(knowledge_mod)

    @pytest.mark.asyncio
    async def test_knowledge_functions_degrade_when_unavailable(self, monkeypatch):
        import core.knowledge as knowledge_mod

        monkeypatch.setattr(knowledge_mod, "_CHROMADB_AVAILABLE", False)

        stats = knowledge_mod.get_knowledge_stats()
        assert stats.get("available") is False
        assert "not installed" in stats.get("reason", "")

        res = await knowledge_mod.search_knowledge("test query")
        assert isinstance(res, dict)
        assert res.get("available") is False

        rec = await knowledge_mod.recall_similar_setups("thesis")
        assert isinstance(rec, dict)
        assert rec.get("available") is False

        ingest_res = knowledge_mod.ingest_knowledge("some text", "book")
        assert isinstance(ingest_res, dict)
        assert ingest_res.get("available") is False

        tm_res = knowledge_mod.ingest_trade_memory({"ticker": "AAPL"})
        assert isinstance(tm_res, dict)
        assert tm_res.get("available") is False

        rag_text, sources = knowledge_mod.get_rag_context("test query")
        assert rag_text == ""
        assert sources == []


class TestMatplotlibOptional:
    def test_backtest_imports_without_matplotlib(self, monkeypatch):
        import mcp_server.backtest as backtest_mod

        # Block matplotlib import
        monkeypatch.setitem(sys.modules, "matplotlib", None)
        monkeypatch.setitem(sys.modules, "matplotlib.pyplot", None)
        monkeypatch.setitem(sys.modules, "matplotlib.dates", None)
        try:
            reloaded = importlib.reload(backtest_mod)
            assert reloaded._MATPLOTLIB_AVAILABLE is False
            assert reloaded.plt is None
        finally:
            monkeypatch.undo()
            importlib.reload(backtest_mod)

    def test_backtest_charts_degrade_when_unavailable(self, monkeypatch):
        import mcp_server.backtest as backtest_mod

        monkeypatch.setattr(backtest_mod, "_MATPLOTLIB_AVAILABLE", False)

        res = backtest_mod.render_equity_chart([], "SPY", "strat", {})
        assert isinstance(res, dict)
        assert res.get("available") is False
        assert "not installed" in res.get("reason", "")

        trade_res = backtest_mod._render_trade_chart(None, [], "SPY", "strat")
        assert isinstance(trade_res, dict)
        assert trade_res.get("available") is False
