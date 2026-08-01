"""Keep docs/API.md honest against the code.

A stale API doc is worse than none: it sends you looking for a route that was
renamed, or promises a tool Claude Desktop will never see. CLAUDE.md says to
regenerate the doc when you add a route or a tool — this is what notices when
you don't.

Also asserts rule 3 structurally rather than by reading the file: the order path
lives in orders.py, and chat.py cannot reach it. A prose rule with no test is a
rule that erodes.
"""

from __future__ import annotations

import asyncio
import os
import re

import pytest

import mcp_server
import server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_PATH = os.path.join(REPO, "docs", "API.md")

# Not part of the documented surface.
SKIP_PATHS = {"/", "/openapi.json", "/static", "/static/{path:path}"}


@pytest.fixture(scope="module")
def doc():
    return open(DOC_PATH, encoding="utf-8").read()


@pytest.fixture(scope="module")
def endpoints():
    return [(m, r.path) for r in server.app.routes if hasattr(r, "methods")
            for m in (r.methods - {"HEAD"}) if r.path not in SKIP_PATHS]


@pytest.fixture(scope="module")
def tool_names():
    return {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}


def _documented(path: str, doc: str) -> bool:
    """The doc may write a path param as {id} rather than the code's name."""
    return path in doc or re.sub(r"\{[a-z_]+\}", "{id}", path) in doc


def test_every_route_is_documented(doc, endpoints):
    paths = {p for _, p in endpoints}
    missing = sorted(p for p in paths if not _documented(p, doc))
    assert not missing, f"routes missing from docs/API.md: {missing}"


def test_every_mcp_tool_is_documented(doc, tool_names):
    missing = sorted(t for t in tool_names if t not in doc)
    assert not missing, f"MCP tools missing from docs/API.md: {missing}"


def test_the_doc_names_no_tool_that_does_not_exist(doc, tool_names):
    prefixes = ("get_", "place_", "cancel_", "preview_", "run_", "create_",
                "delete_", "list_", "add_to_", "remove_from_", "discard_",
                "replace_", "test_")
    cited = {c for c in re.findall(r"`([a-z][a-z0-9_]{3,})`", doc)
             if c.startswith(prefixes)}
    phantom = sorted(cited - tool_names)
    assert not phantom, f"doc names tools that do not exist: {phantom}"


def test_the_header_counts_match(doc, endpoints, tool_names):
    """These are hand-written and the first thing to drift."""
    m = re.search(r"\*\*MCP tools\*\*\s*\((\d+)\)", doc)
    assert m, "could not find the MCP tool count in the doc header"
    assert int(m.group(1)) == len(tool_names), \
        f"doc says {m.group(1)} MCP tools, code has {len(tool_names)}"

    m = re.search(r"\*\*HTTP API\*\*\s*\((\d+) endpoints across (\d+) paths\)", doc)
    assert m, "could not find the HTTP endpoint count in the doc header"
    assert int(m.group(1)) == len(endpoints), \
        f"doc says {m.group(1)} endpoints, code has {len(endpoints)}"
    assert int(m.group(2)) == len({p for _, p in endpoints}), \
        f"doc says {m.group(2)} paths, code has {len({p for _, p in endpoints})}"


# --- rule 3, asserted rather than described --------------------------------


def test_chat_cannot_reach_the_order_path():
    """chat.py reads attacker-controllable text, so it gets no order tools."""
    import chat

    forbidden = {"Bash", "Write", "Edit", "NotebookEdit"}
    gained = set(chat.ALLOWED_TOOLS) & forbidden
    assert not gained, f"chat gained a write tool: {gained}"

    src = open(os.path.join(REPO, "chat.py"), encoding="utf-8").read()
    assert "import orders" not in src and "from orders" not in src, \
        "chat.py imports orders.py — see rule 3 in CLAUDE.md"


def test_broker_writes_live_only_in_orders_py():
    writes = ("place_order", "place_option", "replace_order", "replace_option",
              "cancel_order", "cancel_option", "batch_place_order")
    offenders = []
    for name in os.listdir(REPO):
        if not name.endswith(".py"):
            continue
        # orders.py owns them; wb.py holds the shared client; mcp_server.py
        # names HTTP routes, not SDK calls.
        if name in ("orders.py", "wb.py", "mcp_server.py") or name.startswith("test_"):
            continue
        src = open(os.path.join(REPO, name), encoding="utf-8").read()
        offenders += [f"{name}:{w}" for w in writes if re.search(rf"\.{w}\s*\(", src)]
    assert not offenders, f"broker writes outside orders.py: {offenders}"
