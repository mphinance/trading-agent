# Coding handoff — mechanical tasks

**For:** an implementing model working unattended in `/home/mpha/projects/trading-agent`.
**Written:** 2026-09-03. Everything here is verified against the code; line
numbers were accurate at time of writing but **confirm before editing**.

Work top to bottom. Task 1 and 2 are the priority. If you run out of road, stop
and leave notes rather than guessing.

---

## Rules — read these first, they are not decoration

1. **Do not `git commit`, `git push`, `git checkout`, `git restore`, `git reset`,
   `git stash`, or `git clean`.** There are ~19 files of uncommitted security
   work in the tree already. Leave them alone; do not revert, reformat, or
   "tidy" them. Your changes go on top, uncommitted, for review.
2. **Do not touch these at all:** `vesper/`, `trading_mcp/order_tools.py`,
   `trading_mcp/oauth_provider.py`, `trading_mcp/auth.py`,
   `core/secret_hygiene.py`, `core/approval_registry.py`, `core/halt.py`,
   `core/circuit_breaker.py`, `deploy/`.
3. **Never weaken a test to make it pass.** Several tests in this repo
   deliberately encode security decisions (the AST guard pin in
   `tests/test_trading_mcp.py`, the import-boundary allowlists in
   `tests/test_import_boundaries.py` / `test_import_direction.py`). If one of
   those fails because of your change, **your change is wrong** — revert your
   own edit and write a note. Do not widen an allowlist, do not narrow an AST
   matcher, do not add a skip.
4. **The baseline is `pytest -q` → 706 passed.** Run it before you start to
   confirm, and after every task. If your number is lower, or anything fails,
   fix it or revert your own change before moving on.
5. **Do not install anything.** No `pip install`. `requirements-dev.txt` is
   deliberately not a superset of `requirements.txt` — the Webull and Agent SDKs
   are stubbed in `tests/conftest.py` on purpose. Installing them breaks the
   suite.
6. **Never print, log, or commit a credential.** If you see a value that looks
   like a key or token, do not echo it.
7. **Do not `ssh` anywhere.** All work is local.
8. When stuck, **stop and write a note** in `docs/HANDOFF_NOTES.md` (create it)
   describing exactly where you stopped and why. A half-finished task with a
   clear note is worth more than a guess.

---

## Task 1 — Import-guard two heavy dependencies (highest priority)

### Problem

`mcp_server/server.py` imports all 47 tools at module scope. Two of those paths
import a heavy dependency with no guard, so a partial install raises
`ModuleNotFoundError` at import time and **the entire server fails to start** —
rather than that one tool degrading.

| file | line | offending import |
|---|---|---|
| `core/knowledge.py` | ~20 | `import chromadb` (bare, module scope) |
| `mcp_server/backtest.py` | ~874-877 | `import matplotlib` + `matplotlib.pyplot` + `matplotlib.dates` (bare, module scope) |

Both are pulled in unconditionally by `mcp_server/server.py` (~line 54 for
knowledge, ~line 59 for backtest).

### What to do

Make both imports optional, so that with the dependency **absent**:

- `import mcp_server.server` still succeeds,
- every tool that does not need that dependency still registers and works,
- the tools that *do* need it return a dict `{"available": False, "reason": "<short explanation>"}`
  instead of raising.

The pattern already used elsewhere in this repo is a module-level try/except
setting a flag:

```python
try:
    import chromadb
    _CHROMADB_AVAILABLE = True
except ImportError:          # optional dependency
    chromadb = None
    _CHROMADB_AVAILABLE = False
```

...then an early return in each public function that needs it. Follow that
shape. For `matplotlib` in `backtest.py`, note lines just below the offending
import already guard `quantstats`/`mplfinance` with try/except — **match that
existing style in that file** rather than inventing a new one.

Do not move the imports inside functions if a module-level flag will do; other
code reads these modules' symbols.

### Acceptance criteria

Create `tests/test_optional_dependencies.py` with tests that:

1. Simulate chromadb being absent and assert `core.knowledge` still imports and
   its public functions return `{"available": False, ...}`. Use
   `monkeypatch.setitem(sys.modules, "chromadb", None)` plus
   `importlib.reload`, or patch the module's `_CHROMADB_AVAILABLE` flag —
   whichever you can make reliable. **Do not uninstall anything.**
2. The same for matplotlib and `mcp_server/backtest.py`.
3. An AST or source-level test asserting neither `core/knowledge.py` nor
   `mcp_server/backtest.py` contains a bare module-level `import chromadb` /
   `import matplotlib` outside a `try` block — so this cannot silently regress.

Then: `pytest -q` still shows 706 + your new tests, all passing.

---

## Task 2 — Case-insensitive scrub of production hostnames

### Problem

Files that would ship in a future public repo contain the owner's real
production hostnames. A previous grep was case-sensitive and missed one.

### Exact edits

| file | line | current | replace with |
|---|---|---|---|
| `mcp_server/server.py` | ~878 | `"ghost.mphinance.com"` | `"your-host.example.com"` |
| `mcp_server/server.py` | ~879 | `"ghost.mphinance.com:443"` | `"your-host.example.com:443"` |
| `mcp_server/constellation.py` | ~129 | `"HTTP-Referer": "https://ghost.mphinance.com"` | `"HTTP-Referer": "https://your-host.example.com"` |
| `mcp_server/registry.py` | ~4 | docstring "such as supermcp on Coolify" | "such as a remote MCP server" |
| `.env.trading-agent.example` | — | `MCP_PUBLIC_URL=https://agent.mphinance.com` | `MCP_PUBLIC_URL=https://your-host.example.com` |

**Check first whether those hostnames are load-bearing.** In
`mcp_server/server.py` they appear to be in an allowed-hosts style list — if
changing them would break a running server, **stop and note it** instead of
editing; that one may need a config variable rather than a literal.

### Acceptance criteria

`grep -rIn -i -e "mphinance" -e "coolify" mcp_server/ core/` returns nothing.
`pytest -q` still green at 706.

**Scope limit:** only `mcp_server/`, `core/`, and the two `.env.*.example`
files. Do **not** scrub `docs/`, `deploy/`, `autonomous/`, `CLAUDE.md`, or
`ROADMAP.md` — those are private-repo files and their hostnames are wanted.

---

## Task 3 — Tests for untested modules (do as many as time allows)

These modules ship in the public manifest and have **zero dedicated test
files**. Write one focused test file per module, in this order:

1. `core/screener.py` → `tests/test_screener.py`
2. `core/technicals.py` → `tests/test_technicals.py`
3. `core/vcp_screener.py` → `tests/test_vcp_screener.py`
4. `core/macro_regime.py` → `tests/test_macro_regime.py`
5. `core/market_top.py` → `tests/test_market_top.py`
6. `core/schema.py` → `tests/test_schema.py`
7. `core/cache.py` → `tests/test_cache.py`
8. `core/charts.py` → `tests/test_charts.py`
9. `mcp_server/registry.py` → `tests/test_registry.py`

### Rules for these tests

- **Hermetic. No network, ever.** The whole suite must pass with no internet.
  Mock every HTTP call (`requests`, `httpx`, `yfinance`, `tradingview_screener`).
  Look at existing tests — `tests/test_edgar.py`, `tests/test_options_greeks.py`
  — and copy their mocking style.
- Test **real behaviour**: computation correctness, boundary conditions, what
  happens on malformed or empty input. A test that only asserts
  `callable(func)` is worthless; do not write those.
- For `test_registry.py`: assert `register_momentum_tools` registers the
  expected number of tools onto a `FastMCP` instance, that tool names are
  unique, and that requesting a subset of tiers registers fewer.
- If a module genuinely cannot be tested without network or a credential, write
  the test for the parts that can be, and note the gap in
  `docs/HANDOFF_NOTES.md`.

### Acceptance criteria

Each new file passes on its own (`pytest -q tests/test_<name>.py`) and the full
suite stays green.

---

## Task 4 — Only if 1-3 are done: the public export manifest

Create `scripts/export_public.py` implementing a **dry run only** — it must
build and validate the file list and print it, and must **not** copy, write, or
push anything yet.

The manifest (exact, no globs, no guessing):

```python
PUBLIC_MCP_SERVER = ["mcp_server/"]          # except constellation.py, see below
PUBLIC_CORE_MODULES = [
    "cache", "charts", "conviction", "data", "edgar", "knowledge",
    "macro_regime", "market_top", "options", "options_greeks", "risk",
    "schema", "screener", "technicals", "traderdaddy", "vcp_screener",
]
EXCLUDE = ["mcp_server/constellation.py"]    # dead code: imported by nothing
```

Then `tests/test_public_export.py` asserting: the transitive import closure of
everything in the manifest **never reaches outside the manifest** — in
particular never `vesper`, never `trading_mcp`, and never a `core` module
outside `PUBLIC_CORE_MODULES`. `tests/test_import_boundaries.py` already
contains an AST walk that does most of this; reuse its helpers rather than
writing a new one.

This test is the safety gate for a future public repo, so make it strict: it
should fail loudly if someone adds an import, not shrug.

---

## When you finish

Write `docs/HANDOFF_NOTES.md` containing:

- which tasks you completed and which you did not,
- the exact `pytest -q` result line,
- every place you stopped rather than guessing, and why,
- anything you found that looks wrong but was out of scope.

Leave everything uncommitted. Do not open a PR.
