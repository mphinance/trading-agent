"""M0-01: characterization test for the trading_mcp/mcp_server/vesper split.

Pins every current call-time ``vesper.*`` reference inside ``trading_mcp/``
and ``mcp_server/`` *before* any file in the M0 layering split moves, so that
every later commit's diff to this test is the changelog of the split.

AST-based, not grep-based, on purpose: ``trading_mcp/vesper_tools.py``'s
vesper imports are function-local (deferred so importing the tool module
doesn't eagerly pull in vesper's LangGraph/broker stack) -- a plain
line-anchored grep over the top of the file finds none of them. Walking the
full AST catches them wherever they appear.

Scope note on the reverse direction (vesper -> trading_mcp / vesper ->
mcp_server), recorded here because it was checked against the source, not
assumed from feature_list.json's wording:

- ``vesper/**/*.py`` is asserted to hold **zero** references to
  ``trading_mcp``. That's true today, verified by the same AST walk this
  file uses, and it's the exact property this milestone protects --
  trading_mcp is meant to be a read-only viewer *over* vesper's state, never
  something vesper reaches into.
- ``vesper/**/*.py`` was **not** asserted to hold zero references to
  ``mcp_server`` at M0-01 time: vesper imported mcp_server analytics modules
  at 13 call sites across 10 files then (technicals, options, conviction,
  screener, vcp_screener, charts, data, macro_regime, market_top). M0-02
  moved those specific modules into ``core/`` and repointed every one of
  those 13 sites at ``core.X``, so ``EXPECTED_VESPER_TO_MCP_SERVER_BASELINE``
  below is now the empty dict, verified directly against the source the same
  way the 13-site figure was. The forward-looking assertion that this must
  *stay* empty belongs to M0-07's ``tests/test_import_direction.py`` per its
  own feature_list.json steps ("For vesper -> mcp_server: after M0-02 this
  should be empty. If any site remains, put it in a named allowlist
  constant"); this test still pins the baseline by value so a *new*
  vesper -> mcp_server reference shows up as a diff here too, not only there.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _vesper_refs_in_file(path: Path) -> set[str]:
    """Normalized set of ``vesper.xxx`` references anywhere in ``path``'s
    AST -- function-local imports included, not just module-level ones."""
    tree = ast.parse(path.read_text(), filename=str(path))
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "vesper" or alias.name.startswith("vesper."):
                    refs.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and (
                node.module == "vesper" or node.module.startswith("vesper.")
            ):
                if node.module == "vesper":
                    # `from vesper import audit_chain` -> "vesper.audit_chain"
                    for alias in node.names:
                        refs.add(f"vesper.{alias.name}")
                else:
                    refs.add(node.module)
    return refs


def _refs_to_targets_in_file(path: Path, targets: tuple[str, ...]) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for t in targets:
                    if alias.name == t or alias.name.startswith(t + "."):
                        refs.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                for t in targets:
                    if node.module == t or node.module.startswith(t + "."):
                        refs.add(node.module)
    return refs


def _scan_vesper_refs(root: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        refs = _vesper_refs_in_file(path)
        if refs:
            found[str(path.relative_to(REPO_ROOT))] = refs
    return found


# The exact baseline read off trading_mcp/vesper_tools.py today.
EXPECTED_VESPER_TOOLS_REFS = {
    "vesper.halt",
    "vesper.circuit_breaker",
    "vesper.paper_ledger",
    "vesper.alerts_runner",
    "vesper.bot.inbound",
    "vesper.audit_chain",
    "vesper.monitor",
}


def test_trading_mcp_vesper_imports_match_baseline():
    found = _scan_vesper_refs(REPO_ROOT / "trading_mcp")

    # Only vesper_tools.py references vesper at all under trading_mcp/.
    assert set(found) == {"trading_mcp/vesper_tools.py"}, (
        f"unexpected vesper reference outside vesper_tools.py: {found}"
    )

    actual = found["trading_mcp/vesper_tools.py"]
    assert actual == EXPECTED_VESPER_TOOLS_REFS, (
        "trading_mcp/vesper_tools.py's vesper.* imports changed -- update "
        "this baseline deliberately as part of the M0 split.\n"
        f"expected={EXPECTED_VESPER_TOOLS_REFS}\nactual={actual}"
    )


def test_mcp_server_never_imports_vesper():
    found = _scan_vesper_refs(REPO_ROOT / "mcp_server")
    assert found == {}, f"mcp_server/ must never import vesper: {found}"


def test_vesper_never_imports_trading_mcp():
    """The property this milestone is protecting: trading_mcp is a viewer
    over vesper's state, never a dependency vesper reaches into."""
    offenders: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "vesper").rglob("*.py")):
        refs = _refs_to_targets_in_file(path, ("trading_mcp",))
        if refs:
            offenders[str(path.relative_to(REPO_ROOT))] = refs
    assert offenders == {}, f"vesper/ must never import trading_mcp: {offenders}"


# As of M0-01, this pinned 13 real vesper -> mcp_server import sites across
# 10 files: mcp_server was both the quant-analytics library vesper legitimately
# depended on AND the MCP registration layer. M0-02 split those apart --
# technicals, options, conviction, screener, vcp_screener, charts, data,
# macro_regime and market_top (plus wb/md/td/edgar/quotes) moved into core/,
# and every one of those 13 sites now imports core.X instead. Verified
# directly against the source (this same AST walk, not inferred from any
# doc): vesper/**/*.py holds zero mcp_server references today, so the
# baseline is empty, per this file's own M0-01-era note that said "ideally
# to {}". Enforced going forward by M0-07's test_import_direction.py, which
# owns the vesper -> core assertion this test doesn't make.
EXPECTED_VESPER_TO_MCP_SERVER_BASELINE: dict[str, set[str]] = {}


def test_vesper_to_mcp_server_baseline_is_pinned_not_growing():
    actual: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "vesper").rglob("*.py")):
        refs = _refs_to_targets_in_file(path, ("mcp_server",))
        if refs:
            actual[str(path.relative_to(REPO_ROOT))] = refs
    assert actual == EXPECTED_VESPER_TO_MCP_SERVER_BASELINE, (
        "vesper -> mcp_server imports changed. If this shrank, good -- "
        "update the baseline (and check whether M0-02/M0-07 can now be "
        "narrowed too). If it grew, that's a new reverse-layering "
        f"dependency -- don't add it here without a reason.\n"
        f"expected={EXPECTED_VESPER_TO_MCP_SERVER_BASELINE}\nactual={actual}"
    )
