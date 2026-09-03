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
#
# M0-03 moved halt.py/circuit_breaker.py/paper_ledger.py/audit_chain.py into
# core/ (the four pure state-I/O modules), and vesper_tools.py's four
# corresponding function-local imports were repointed at core.X per that
# feature's own steps -- so vesper.halt, vesper.circuit_breaker,
# vesper.paper_ledger and vesper.audit_chain drop out of this baseline.
# M0-04 did the same to vesper.bot.inbound: ApprovalRegistry moved to
# core/approval_registry.py, and vesper_tools.py's two `from
# vesper.bot.inbound import approval_registry` sites (list_pending_proposals,
# get_proposal) became `from core.approval_registry import
# approval_registry` -- the whole point being that these read-only tools no
# longer need to import vesper.bot at all (that package's __init__ eagerly
# constructs TelegramAdapter/DiscordAdapter/WebhookAdapter/ChannelManager).
# So vesper.bot.inbound drops out here too.
# vesper.alerts_runner dropped out as of M0-06: its only trading_mcp
# consumer was `list_alerts`'s `_build_levels_of()` (a pure, side-effect-free
# wrapper around `TDPro.levels()` -- module-level imports in
# vesper/alerts_runner.py are just logging/typing, and _build_levels_of()
# only ever resolved to `core.td` at call time, verified against the source,
# not assumed). That logic moved to `core.td.build_levels_of()`, which
# vesper/alerts_runner.py's own `_build_levels_of()` now delegates to (so
# `build_watcher()` and the live watcher thread are unchanged), and
# vesper_tools.py's `list_alerts` imports it from `core.td` directly. That
# was the last vesper.* reference in this file, so the baseline is now
# empty -- trading_mcp/vesper_tools.py imports nothing from vesper/ at all.
# vesper.monitor dropped out as of M0-05: importing it -- even just to reach
# its two read-only methods -- pulls `vesper.execution_guard`'s live `guard`
# singleton into sys.modules as an import side effect (module-scope `from
# vesper.execution_guard import guard, GuardError, TradingDisabled`), which
# this read-only server must never do. The position-monitor-preview tool now
# reads through core/position_preview.py, a guard-free duplicate of just the
# read-only rules, instead.
EXPECTED_VESPER_TOOLS_REFS: set[str] = set()

# Amendment A4 (app_spec.txt, CLAUDE.md rule 3) landed AFTER the M0-06 baseline
# above was driven to empty, and it deliberately reopens exactly two doors —
# no more. Both are keyed by module, because "which file" is the whole control:
# the point of A4 is that the order path is reachable from ONE named module a
# reviewer can hold in their head, not from anywhere under trading_mcp/.
#
#   order_tools.py  the sanctioned order path. Imports `vesper.execution_guard`
#                   because A4's other half is that execution code is NEVER
#                   duplicated -- a second implementation of the guards would be
#                   far worse than this import. `vesper.risk` comes with it for
#                   deterministic sizing.
#   drafting.py     draft_proposal (M8-15..18). Builds an OrderProposal and runs
#                   the deterministic risk check; it CANNOT place, and the pin
#                   below still forbids it `vesper.execution_guard`.
#
# This is a widening of a security boundary, so it is spelled out per-module and
# per-import rather than as a blanket "trading_mcp may import vesper".
A4_SANCTIONED_VESPER_REFS: dict[str, set[str]] = {
    "trading_mcp/order_tools.py": {"vesper.execution_guard", "vesper.risk"},
    "trading_mcp/drafting.py": {"vesper.risk", "vesper.state", "vesper.bot.manager"},
}

# Belt-and-suspenders on top of the exact-match assertion below: even if a
# future change legitimately widens EXPECTED_VESPER_TOOLS_REFS again (say,
# a new vesper.* module as pure and justified as alerts_runner once was),
# these three must never reappear in trading_mcp/ under any name. Spelled
# out explicitly per M0-06's own steps, rather than relying solely on the
# baseline's set-equality to catch a regression here.
FORBIDDEN_VESPER_REFS = {
    "vesper.monitor",
    "vesper.bot.inbound",
    "vesper.execution_guard",
}

# The one module A4 exempts from FORBIDDEN_VESPER_REFS. Kept as a separate
# constant so the exemption is a named, greppable, one-line thing rather than a
# hole quietly punched in the set above.
A4_ORDER_PATH_MODULE = "trading_mcp/order_tools.py"


def test_trading_mcp_vesper_imports_match_baseline():
    found = _scan_vesper_refs(REPO_ROOT / "trading_mcp")

    # The full expected picture: the (empty) M0-06 baseline for vesper_tools.py,
    # plus A4's two named modules. Anything else referencing vesper.* is a new
    # door into the order path and fails here.
    expected: dict[str, set[str]] = dict(A4_SANCTIONED_VESPER_REFS)
    if EXPECTED_VESPER_TOOLS_REFS:
        expected["trading_mcp/vesper_tools.py"] = EXPECTED_VESPER_TOOLS_REFS

    assert found == expected, (
        "trading_mcp/ -> vesper imports changed. Nothing may reference vesper.* "
        "except the modules amendment A4 names (order_tools.py, drafting.py) and "
        "only for the imports listed there -- if a new tool needs something from "
        "vesper/, move that piece into core/ first, the way M0-05/M0-06 did.\n"
        f"expected={expected}\nactual={found}"
    )

    # M0-06 + A4: explicit, name-based denial on top of the baseline's set
    # equality. `vesper.execution_guard` is exempt in exactly ONE module --
    # everywhere else under trading_mcp/, importing it is the "new threat model"
    # sentence in rule 3, and `vesper.monitor` / `vesper.bot.inbound` remain
    # forbidden with no exemption at all.
    for module, refs in found.items():
        forbidden_here = set(FORBIDDEN_VESPER_REFS)
        if module == A4_ORDER_PATH_MODULE:
            forbidden_here.discard("vesper.execution_guard")
        forbidden_hit = refs & forbidden_here
        assert not forbidden_hit, (
            f"{module} must never import these: {forbidden_hit}"
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
