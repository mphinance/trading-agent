"""trading_mcp: owner-only MCP server exposing this repo's READ-ONLY tooling.

Separate from mcp_server/ (the stdio "momentum" server, left untouched) and
from supermcp (a different, subscriber-facing server on another host — this
package does not talk to it). See CLAUDE.md rule 3: the order path lives
only in vesper/execution_guard.py, and nothing here calls it.
"""

__version__ = "0.1.0"
