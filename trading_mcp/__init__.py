"""trading_mcp: owner-only MCP server exposing this repo's trading tooling.

No longer read-only as of Amendment A4 (2026-09-04): order_tools.py in this
package is the ONLY module here permitted to reach vesper.execution_guard,
behind require_scopes("trade"). Every other module in this package stays
read-only. execution_guard.py remains the sole module in the whole repo that
can move money (CLAUDE.md rule 3) — order_tools.py calls it, it does not
duplicate it.

Separate from mcp_server/ (the stdio "momentum" server, left untouched) and
from supermcp (a different, subscriber-facing server on another host — this
package does not talk to it).
"""

__version__ = "0.1.0"
