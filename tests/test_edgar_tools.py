"""M3-07: a missing credential still degrades rather than crashing (EDGAR
surface, wrapper level).

tests/test_edgar.py::test_get_raises_clear_error_without_user_agent already
pins that core.edgar._get() raises a clean RuntimeError naming SEC_USER_AGENT
rather than some opaque requests/urllib error. But nothing before this file
exercised the MCP-tool layer built on top of it: mcp_server/edgar_tools.py's
wrappers each catch that exception and return {"ticker": ..., "error": str(e)}
per their own module docstring ("a raised exception from a tool call is a
worse user experience than a clear error dict"). This is the other half of
that promise -- that the catch is real, not just documented.
"""

from __future__ import annotations

import pytest

from mcp_server.edgar_tools import get_sec_filings


@pytest.mark.asyncio
async def test_get_sec_filings_degrades_when_user_agent_unset(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    result = await get_sec_filings("AAPL")

    assert result["ticker"] == "AAPL"
    assert "error" in result
    assert "SEC_USER_AGENT" in result["error"]
    assert "filings" not in result
