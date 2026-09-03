# Handoff Notes — Mechanical Coding Tasks

**Session Date:** 2026-09-03  
**Status:** Tasks 1, 2, 3, and 4 Completed. All work uncommitted.

---

## 1. Tasks Completed

### Task 1 — Import-guard heavy dependencies (`chromadb`, `matplotlib`)
- **`core/knowledge.py`**:
  - Guarded `import chromadb` behind `try ... except ImportError`, exposing `_CHROMADB_AVAILABLE` flag.
  - Guarded internal `_get_chroma()` with `RuntimeError` if invoked without `chromadb`.
  - Guarded public functions: `search_knowledge`, `recall_similar_setups`, `ingest_knowledge`, `ingest_trade_memory`, `get_knowledge_stats` return `{"available": False, "reason": "chromadb optional dependency is not installed"}`.
  - Guarded `get_rag_context` to return `("", [])`.
- **`mcp_server/backtest.py`**:
  - Guarded `import matplotlib`, `matplotlib.pyplot`, `matplotlib.dates` behind `try ... except ImportError`, exposing `_MATPLOTLIB_AVAILABLE` flag.
  - Guarded `_render_equity_chart` and `_render_trade_chart` to return `{"available": False, "reason": "matplotlib optional dependency is not installed"}` when called without matplotlib.
  - Aliased public helper `render_equity_chart = _render_equity_chart`.
  - Updated `backtest_strategy` so backtests run and calculate all metrics even without matplotlib.
- **Tests Added**: `tests/test_optional_dependencies.py` (6 tests covering AST module-level checks, mock import failure reloads, and graceful degradation).

### Task 2 — Case-insensitive scrub of production hostnames
- **`mcp_server/server.py`**: Replaced literal `"ghost.mphinance.com"` with `"your-host.example.com"`. Made reverse-proxy allowed hosts configurable via `MCP_ALLOWED_HOSTS` environment variable (default: `"your-host.example.com,your-host.example.com:443"`) so running servers can set their domain without code changes.
- **`mcp_server/constellation.py`**: Replaced `"https://ghost.mphinance.com"` with `"https://your-host.example.com"`.
- **`mcp_server/registry.py`**: Updated docstring to `"such as a remote MCP server"`.
- **`.env.trading-agent.example`**: Updated `MCP_PUBLIC_URL=https://your-host.example.com` and stripped Coolify reference.
- **Verification**: `grep -rIn -i -e "mphinance" -e "coolify" mcp_server/` returns **0 matches**.

### Task 3 — Unit tests for untested public modules
Added hermetic test suites with zero external network dependencies, mocking all remote calls:
1. `core/screener.py` → `tests/test_screener.py` (10 tests: sanitization, preset definitions, mock TV queries, custom filters)
2. `core/technicals.py` → `tests/test_technicals.py` (5 tests: pandas-ta indicators, EMA stack, insufficient bar handling, analysis strings)
3. `core/vcp_screener.py` → `tests/test_vcp_screener.py` (4 tests: Stage 2 Minervini template, volatility contraction depth detection, empty results)
4. `core/macro_regime.py` → `tests/test_macro_regime.py` (3 tests: cross-asset ratio calculation, Goldilocks and Risk-Off regime classifications, missing data)
5. `core/market_top.py` → `tests/test_market_top.py` (3 tests: distribution days, stalling days, leadership health, defensive sector rotation, composite scoring)
6. `core/schema.py` → `tests/test_schema.py` (4 tests: `SignalResult` model, factory methods, serialization, validation errors)
7. `core/cache.py` → `tests/test_cache.py` (8 tests: market status, holiday/weekend TTL, deterministic key generation, singleflight request coalescing, cache hit/miss)
8. `core/charts.py` → `tests/test_charts.py` (3 tests: candlestick rendering, EMA stack overlays, base64 PNG validation)
9. `mcp_server/registry.py` → `tests/test_registry.py` (6 tests: 47 unique tools registered across Tiers 1-3, tier subsets, output helper)

### Task 4 — Public export manifest & boundary test
- **`scripts/export_public.py`**: Created dry-run validator with explicit constants `PUBLIC_MCP_SERVER`, `PUBLIC_CORE_MODULES`, and `EXCLUDE = ["mcp_server/constellation.py"]`. Validates that all 44 manifest files exist (27 `mcp_server/` files, 17 `core/` files). Prints breakdown of 12,776 lines of code.
- **`tests/test_public_export.py`**: Strict AST boundary test suite (7 tests) asserting that the transitive closure of the 44 manifest files never imports:
  - `vesper` or `vesper.*` (0 violations)
  - `trading_mcp` or `trading_mcp.*` (0 violations)
  - Unapproved `core.*` modules outside the 16 approved ones (e.g. `core.wb`, `core.md`, `core.td`, `core.approval_registry`, `core.halt`, `core.circuit_breaker`, `core.paper_ledger`, `core.audit_chain`, `core.position_preview`, `core.metrics`, `core.quotes`, `core.secret_hygiene`) (0 violations)
  - Excluded `mcp_server/constellation.py` (0 violations)

---

## 2. Exact Test Suite Result

```
765 passed in 35.55s
```
- **Baseline**: 706 passed
- **New tests added**: 59 passed (6 in Task 1, 46 in Task 3, 7 in Task 4)
- **Total green**: 765 passed, 0 failures, 0 regressions.

---

## 3. Places Stopped Rather Than Guessing

None — all tasks in the sequence (Tasks 1–4) were successfully completed and confirmed by tests.

---

## 4. Out-of-Scope Findings

1. **`core/charts.py` bare matplotlib import**:
   - `core/charts.py:17-18` imports `matplotlib` and calls `matplotlib.use("Agg")` at module scope with no try/except guard.
   - While Task 1 specifically scoped `core/knowledge.py` and `mcp_server/backtest.py`, any direct import of `core.charts` in a bare environment without `matplotlib` will raise `ModuleNotFoundError`.
   - Recommendation: Guard `core/charts.py` with the same `_MATPLOTLIB_AVAILABLE` pattern before publishing `quant-mcp`.
2. **`core/secret_hygiene.py` docstring**:
   - Line 4 contains `agent.mphinance.com`. Because Rule 2 strictly prohibited modifying `core/secret_hygiene.py`, it was not touched.
   - Fortunately, `core/secret_hygiene.py` is part of the private safety layer and never ships in the public export manifest.
3. **`core/cache.py` unretrieved future exception warning**:
   - In `core/cache.py:180`, `future.set_exception(exc)` sets the exception on an in-flight coalescing future. When there are no other concurrent callers awaiting that future, Python asyncio emits a `Future exception was never retrieved` warning during garbage collection. It does not affect execution or test correctness.
