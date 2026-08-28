"""Momentum MCP Server standalone entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure project root is always on sys.path and .env is loaded
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from mcp_server.server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
