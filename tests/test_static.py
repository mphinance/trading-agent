"""Checks on the single-page UI.

There is no bundler and no build step, so nothing else would catch a syntax
error in `static/index.html` before it reaches the browser — and a parse error
there is total: the whole panel goes blank, not just the broken feature. That
has already happened once, when a `function money()` was added alongside the
existing `const money` in the same scope.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"
HTML = INDEX.read_text()
SCRIPT = re.search(r"<script>(.*)</script>", HTML, re.S).group(1)


def test_every_element_id_the_script_touches_exists_in_the_markup():
    """`$("typo")` returns null and dies at the first property access."""
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    used = set(re.findall(r'\$\("([^"]+)"\)', SCRIPT))
    assert not (used - ids), f"script references missing ids: {sorted(used - ids)}"


def test_no_identifier_is_declared_twice_at_top_level():
    """const/let/function collisions in one scope are a parse error, not a warning."""
    decls = re.findall(r'^(?:const|let|function)\s+([A-Za-z_$][\w$]*)', SCRIPT, re.M)
    dupes = {n for n in decls if decls.count(n) > 1}
    assert not dupes, f"duplicate top-level declarations would fail to parse: {sorted(dupes)}"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_script_parses(tmp_path):
    js = tmp_path / "page.js"
    js.write_text(SCRIPT)
    proc = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_secret_scrub_covers_the_key_shapes_that_appear_in_this_repo():
    """Rule 5: the panel is streamed, so the scrub is the guarantee, not the prompt."""
    scrub = re.search(r"const SECRET_RE = /(.+?)/g", SCRIPT).group(1)
    assert "sk-ant-" in scrub and "td_live_" in scrub


def test_no_hardcoded_credentials_in_the_page():
    for pattern in (r"sk-ant-[A-Za-z0-9]{10,}", r"td_live_[A-Za-z0-9]{10,}",
                    r"\d{8,}:AA[A-Za-z0-9_-]{20,}"):
        assert not re.search(pattern, HTML), f"credential-shaped string matching {pattern}"


def test_the_alert_row_select_does_not_inherit_the_full_width_rule():
    """A flex child that cannot shrink pushes its siblings out of the card.

    `select { width: 100% }` exists for the chat model picker. In `.gex-head`
    that made flex-basis:auto resolve to the whole row, and `flex: 0 0` then
    refused to shrink it — the Arm button ended up 135px outside its card and
    the entire page scrolled sideways. The fix is an explicit `width` on the
    scoped rule; this pins it so the global rule cannot leak back in.
    """
    # Anchored to the standalone rule: an unanchored search also matches the
    # ".gex-head input, .gex-head select" block above it, which sets flex: 1
    # and would pass this check while the real, later rule still overflowed.
    rule = re.search(r"^\s*\.gex-head select\s*\{([^}]*)\}", HTML, re.M)
    assert rule, ".gex-head select rule is gone — did the alert form move?"
    body = rule.group(1)
    assert "width:" in body, ".gex-head select must set its own width"
    flex = re.search(r"flex:\s*([^;]+);", body)
    assert flex, ".gex-head select must set flex explicitly"
    grow, shrink, *_ = flex.group(1).split()
    assert shrink != "0", f"select must be allowed to shrink, got flex: {flex.group(1)}"
