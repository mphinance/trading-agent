"""Keep docs/API.md honest against the code.

A stale API doc is worse than none: it sends you looking for a route that was
renamed, or promises a tool Claude Desktop will never see. CLAUDE.md tells you
to regenerate the doc when you add a route or a tool — this is what notices when
you don't.

Checks both directions, plus the counts in the header, which are the first thing
to drift and the easiest thing to get wrong by hand.

No network and no credentials: importing `server` and `mcp_server` builds the
route table and the tool registry without touching Webull.
"""

import asyncio
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# alerts.py resolves its store path at import; keep a test run off the real one.
os.environ.setdefault("SIDECAR_STATE_DIR", tempfile.mkdtemp(prefix="sidecar-test-docs-"))

import mcp_server  # noqa: E402
import server  # noqa: E402

DOC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "API.md")

# Not part of the documented surface.
SKIP_PATHS = {"/", "/openapi.json", "/static", "/static/{path:path}"}


def check(label, fn):
    try:
        fn()
        print(f"  PASS  {label}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {label}: {e}")
        return False
    except Exception as e:
        print(f"  ERROR {label}: {type(e).__name__}: {e}")
        return False


doc = open(DOC, encoding="utf-8").read()

endpoints = [(m, r.path) for r in server.app.routes if hasattr(r, "methods")
             for m in (r.methods - {"HEAD"}) if r.path not in SKIP_PATHS]
paths = {p for _, p in endpoints}
tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}

ok = []
print("\n== docs/API.md vs the code ==")


def documented(path: str) -> bool:
    """The doc may write a path param as {id} rather than the code's name."""
    return path in doc or re.sub(r"\{[a-z_]+\}", "{id}", path) in doc


def t_routes_documented():
    missing = sorted(p for p in paths if not documented(p))
    assert not missing, f"routes missing from docs/API.md: {missing}"
ok.append(check(f"all {len(paths)} HTTP paths appear in the doc", t_routes_documented))


def t_tools_documented():
    missing = sorted(t for t in tools if t not in doc)
    assert not missing, f"MCP tools missing from docs/API.md: {missing}"
ok.append(check(f"all {len(tools)} MCP tools appear in the doc", t_tools_documented))


def t_no_phantom_tools():
    """Nothing in the doc should look like a tool that doesn't exist."""
    prefixes = ("get_", "place_", "cancel_", "preview_", "run_", "create_",
                "delete_", "list_", "add_to_", "remove_from_", "discard_",
                "replace_", "test_")
    cited = {c for c in re.findall(r"`([a-z][a-z0-9_]{3,})`", doc)
             if c.startswith(prefixes)}
    phantom = sorted(cited - tools)
    assert not phantom, f"doc names tools that do not exist: {phantom}"
ok.append(check("no phantom tool names in the doc", t_no_phantom_tools))


def t_counts_match():
    m = re.search(r"\*\*MCP tools\*\*\s*\((\d+)\)", doc)
    assert m, "could not find the MCP tool count in the doc header"
    assert int(m.group(1)) == len(tools), \
        f"doc says {m.group(1)} MCP tools, code has {len(tools)}"

    m = re.search(r"\*\*HTTP API\*\*\s*\((\d+) endpoints across (\d+) paths\)", doc)
    assert m, "could not find the HTTP endpoint count in the doc header"
    assert int(m.group(1)) == len(endpoints), \
        f"doc says {m.group(1)} endpoints, code has {len(endpoints)}"
    assert int(m.group(2)) == len(paths), \
        f"doc says {m.group(2)} paths, code has {len(paths)}"
ok.append(check(f"header counts match ({len(tools)} tools, "
                f"{len(endpoints)} endpoints, {len(paths)} paths)", t_counts_match))


print("\n== the order path stays where CLAUDE.md says it is ==")


def t_chat_has_no_order_tools():
    """Rule 3: chat.py reads attacker-controllable text, so it gets no order tools."""
    import chat
    forbidden = {"Bash", "Write", "Edit", "NotebookEdit"}
    assert not (set(chat.ALLOWED_TOOLS) & forbidden), \
        f"chat gained a write tool: {set(chat.ALLOWED_TOOLS) & forbidden}"
    src = open(os.path.join(os.path.dirname(DOC), "..", "chat.py"), encoding="utf-8").read()
    assert "import orders" not in src and "from orders" not in src, \
        "chat.py imports orders.py — see rule 3 in CLAUDE.md"
ok.append(check("chat.py cannot reach orders.py", t_chat_has_no_order_tools))


def t_orders_is_the_only_writer():
    """Every broker write should live in orders.py and nowhere else."""
    root = os.path.dirname(os.path.abspath(__file__))
    writes = ("place_order", "place_option", "replace_order", "replace_option",
              "cancel_order", "cancel_option", "batch_place_order")
    offenders = []
    for name in os.listdir(root):
        if not name.endswith(".py") or name in ("orders.py", "wb.py"):
            continue
        if name.startswith("test_") or name in ("mcp_server.py",):
            continue  # tests stub them; mcp_server names HTTP routes, not SDK calls
        src = open(os.path.join(root, name), encoding="utf-8").read()
        for w in writes:
            if re.search(rf"\.{w}\s*\(", src):
                offenders.append(f"{name}:{w}")
    assert not offenders, f"broker writes outside orders.py: {offenders}"
ok.append(check("broker writes live only in orders.py", t_orders_is_the_only_writer))


print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
