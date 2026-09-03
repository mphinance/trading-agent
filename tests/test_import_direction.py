"""M0-07: discovery-based AST test enforcing the full layering, forward-looking.

``tests/test_import_boundaries.py`` (M0-01/M0-06) already pins the
trading_mcp <-> vesper boundary in detail -- this file's job is the
*complete* four-package DAG: ``core/`` is meant to be the bottom layer, and
``vesper``, ``mcp_server`` and ``trading_mcp`` are all meant to sit above it
without ever pointing back down at each other in the wrong direction.

Target layering (arrows are "imports from", pointing down the stack)::

    trading_mcp
         |
    vesper   mcp_server
         \\      /
           core

So the four forbidden directions are: core -> {vesper, mcp_server,
trading_mcp}; vesper -> trading_mcp; mcp_server -> {vesper, trading_mcp}.
vesper -> mcp_server is the one edge this milestone treats as a *migration
in progress* rather than a flat prohibition -- M0-02 moved the quant
analytics/data modules that vesper needed out of mcp_server/ and into
core/, and M0-01's ``EXPECTED_VESPER_TO_MCP_SERVER_BASELINE`` in
test_import_boundaries.py already pins that baseline at ``{}``. This file
re-derives that baseline itself (not just re-imports the other test's
constant) so a new vesper -> mcp_server import site fails *here* too, with
its own explicit, shrinking allowlist mechanism, exactly as M0-07's own
steps ask for.

Checked directly against the source before writing any assertion below
(the standing warning: a doc agreeing with another doc proves nothing) --
see the accompanying claude-progress.txt entry for what core/ actually
imported before this feature moved six mcp_server leaf modules
(cache/schema/risk/options_greeks/traderdaddy/knowledge) and one vesper
module (metrics) down into core/ to make the "core imports nothing above
it" assertion true rather than aspirational.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _import_targets_in_file(path: Path) -> set[str]:
    """Every top-level dotted module name imported anywhere in ``path``'s
    AST (module-level or function-local -- a plain top-of-file grep would
    miss deferred imports, which several modules in this repo use
    deliberately to avoid pulling in heavy optional dependencies at import
    time)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # node.level > 0 is a relative import (e.g. `from . import x`
            # inside the package itself) -- never a cross-package reference,
            # so it can't violate any of this file's directional rules.
            if node.level == 0 and node.module:
                targets.add(node.module.split(".")[0])
    return targets


def _refs_to_packages(root: Path, packages: tuple[str, ...]) -> dict[str, set[str]]:
    """Map of {relative file path: {package names it imports}} for every
    file under ``root`` that imports any of ``packages`` at its top level
    (e.g. importing ``vesper`` covers ``import vesper.foo`` and
    ``from vesper.foo import bar`` alike, since both resolve to the
    ``vesper`` package first)."""
    found: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        hit = _import_targets_in_file(path) & set(packages)
        if hit:
            found[str(path.relative_to(REPO_ROOT))] = hit
    return found


def test_core_imports_nothing_above_it():
    """core/ is the bottom of the stack. Nothing under it may import
    vesper, mcp_server or trading_mcp -- there is no allowlist for this
    direction, unlike vesper -> mcp_server below. If a new core/ module
    needs something from one of those three packages, the fix is to move
    the needed piece down into core/ (as this feature did for
    cache/schema/risk/options_greeks/traderdaddy/knowledge and metrics),
    not to add an exception here."""
    found = _refs_to_packages(REPO_ROOT / "core", ("vesper", "mcp_server", "trading_mcp"))
    assert found == {}, f"core/ must import nothing above it: {found}"


def test_mcp_server_never_imports_vesper_or_trading_mcp():
    found = _refs_to_packages(REPO_ROOT / "mcp_server", ("vesper", "trading_mcp"))
    assert found == {}, f"mcp_server/ must never import vesper or trading_mcp: {found}"


def test_vesper_never_imports_trading_mcp():
    found = _refs_to_packages(REPO_ROOT / "vesper", ("trading_mcp",))
    assert found == {}, f"vesper/ must never import trading_mcp: {found}"


# vesper -> mcp_server: the one direction this milestone treats as a
# migration rather than a flat ban, per M0-07's own feature_list.json steps
# ("after M0-02 this should be empty. If any site remains, put it in a
# named allowlist constant with a comment saying why, and assert the set
# never grows"). Verified empty directly against the source (the AST walk
# below, not by trusting test_import_boundaries.py's own copy of this same
# fact) as of this commit -- every module vesper used to reach into
# mcp_server for (technicals, options, conviction, screener, vcp_screener,
# charts, data, macro_regime, market_top via M0-02; cache, schema, risk,
# options_greeks, traderdaddy, knowledge via this feature) now lives in
# core/ instead.
VESPER_TO_MCP_SERVER_ALLOWLIST: dict[str, set[str]] = {}


def test_vesper_to_mcp_server_allowlist_never_grows():
    actual: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "vesper").rglob("*.py")):
        hit = _import_targets_in_file(path) & {"mcp_server"}
        if hit:
            actual[str(path.relative_to(REPO_ROOT))] = hit
    assert actual == VESPER_TO_MCP_SERVER_ALLOWLIST, (
        "vesper -> mcp_server imports changed. The allowlist above is "
        "empty and should stay that way -- if this is a genuinely new, "
        "justified case, add it to VESPER_TO_MCP_SERVER_ALLOWLIST by name "
        "with a comment explaining why it can't move into core/ instead, "
        "the same way M0-02/this feature closed every prior case.\n"
        f"expected={VESPER_TO_MCP_SERVER_ALLOWLIST}\nactual={actual}"
    )


# trading_mcp -> vesper: the allowlist M0-06 drove to empty
# (tests/test_import_boundaries.py's EXPECTED_VESPER_TOOLS_REFS). Re-derived
# here from source, independently of that test's own constant, so a
# regression is caught even if someone edits the other file's baseline
# without noticing this one.
#
# Reopened deliberately by amendment A4 (app_spec.txt, CLAUDE.md rule 3), which
# postdates M0-06: the MCP surface may reach the order path via
# `vesper.execution_guard` ONLY, from the single designated module
# `trading_mcp/order_tools.py`, precisely so execution code is never duplicated.
# `drafting.py` carries the draft_proposal path (build + deterministic risk
# check + channel broadcast) and cannot place. This file only records WHICH
# top-level package each module reaches into; tests/test_import_boundaries.py
# holds the finer-grained per-symbol pin and the "execution_guard is exempt in
# exactly one module" rule.
TRADING_MCP_TO_VESPER_ALLOWLIST: dict[str, set[str]] = {
    "trading_mcp/drafting.py": {"vesper"},
    "trading_mcp/order_tools.py": {"vesper"},
}


def test_trading_mcp_vesper_imports_match_m0_06_allowlist():
    actual: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "trading_mcp").rglob("*.py")):
        hit = _import_targets_in_file(path) & {"vesper"}
        if hit:
            actual[str(path.relative_to(REPO_ROOT))] = hit
    assert actual == TRADING_MCP_TO_VESPER_ALLOWLIST, (
        "trading_mcp/ -> vesper imports changed from the M0-06 baseline. "
        "trading_mcp is a read-only viewer over vesper's state (rule 3) -- "
        "if a new tool genuinely needs something from vesper/ that isn't "
        "already in core/, move the needed piece into core/ first, the way "
        "M0-05/M0-06 did for position_preview and alerts_runner's "
        "build_levels_of().\n"
        f"expected={TRADING_MCP_TO_VESPER_ALLOWLIST}\nactual={actual}"
    )
