"""Comprehensive verification script for:
1. TraderDaddy Pro MCP
2. Webull OpenAPI MCP
3. TickerTrace Pro MCP
4. Momentum MCP (Quantitative Analysis & Screening)
"""

import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def test_tdpro():
    print("=" * 60)
    print("1. Testing TraderDaddy Pro MCP Integration")
    print("=" * 60)
    try:
        from core.td import TDPro
        td = TDPro()
        if not td.configured:
            print("[FAIL] TDPro is not configured (TD_API_KEY missing)")
            return False
        
        print("-> Calling TDPro tool 'get_market_health'...")
        res = td.call("get_market_health")
        print("[SUCCESS] TDPro get_market_health response received!")
        if isinstance(res, dict):
            print("   Market Health Score:", res.get("compositeScore", {}))
        
        print("-> Calling TDPro tool 'get_gex_ticker' for SPY...")
        spy_levels = td.levels("SPY")
        print(f"[SUCCESS] SPY Levels: Spot={spy_levels.get('spot')}, Regime={spy_levels.get('regime')}, Gamma Flip={spy_levels.get('flip')}")
        return True
    except Exception as e:
        print(f"[FAIL] TDPro MCP test failed: {e}")
        return False


async def test_webull_mcp():
    print("\n" + "=" * 60)
    print("2. Testing Webull OpenAPI MCP Server & Tools")
    print("=" * 60)
    try:
        from webull_openapi_mcp.config import load_config
        from webull_openapi_mcp.server import build_server

        config = load_config(str(Path(__file__).parent / ".env"))
        server = build_server(config)
        
        async with server._lifespan_manager():
            # Test account tool
            print("-> Calling Webull MCP tool 'get_account_list'...")
            acc_res = await server.call_tool("get_account_list", {})
            print("[SUCCESS] Webull get_account_list:")
            for line in acc_res.content[0].text.splitlines():
                if "ID:" in line or "=== Account" in line:
                    print("  ", line)

            # Test market data tool
            print("-> Calling Webull MCP tool 'get_stock_snapshot' for AAPL...")
            snap_res = await server.call_tool("get_stock_snapshot", {"symbols": "AAPL"})
            print("[SUCCESS] Webull get_stock_snapshot:")
            for line in snap_res.content[0].text.splitlines()[:5]:
                if line.strip():
                    print("  ", line)

        return True
    except Exception as e:
        print(f"[FAIL] Webull OpenAPI MCP test failed: {e}")
        return False


async def test_tickertrace_mcp():
    print("\n" + "=" * 60)
    print("3. Testing TickerTrace Pro MCP Server & Tools")
    print("=" * 60)
    try:
        from tickertrace_mcp import mcp as tt_mcp
        
        tools = await tt_mcp.list_tools()
        print(f"[SUCCESS] TickerTrace MCP registered {len(tools)} tools.")

        print("-> Calling TickerTrace tool 'get_briefing'...")
        briefing_res = await tt_mcp.call_tool("get_briefing", {})
        print("[SUCCESS] TickerTrace get_briefing received!")

        print("-> Calling TickerTrace tool 'get_stock_activity' for AVGO...")
        stock_res = await tt_mcp.call_tool("get_stock_activity", {"ticker": "AVGO"})
        print("[SUCCESS] TickerTrace get_stock_activity(AVGO):")
        import json
        stock_data = json.loads(stock_res.content[0].text)
        print(f"   Name: {stock_data.get('name')}, Sector: {stock_data.get('sector')}, Tracked Funds: {stock_data.get('fundCount')}")
        return True
    except Exception as e:
        print(f"[FAIL] TickerTrace Pro MCP test failed: {e}")
        return False


async def test_momentum_mcp():
    print("\n" + "=" * 60)
    print("4. Testing Momentum MCP Server & Quantitative Tools")
    print("=" * 60)
    try:
        from mcp_server.server import mcp as mom_mcp
        
        tools = await mom_mcp.list_tools()
        print(f"[SUCCESS] Momentum MCP registered {len(tools)} tools.")

        print("-> Calling Momentum tool 'analyze_technicals' for AAPL...")
        tech_res = await mom_mcp.call_tool("analyze_technicals", {"ticker": "AAPL"})
        print("[SUCCESS] Momentum analyze_technicals(AAPL) succeeded:")
        if hasattr(tech_res, "content") and tech_res.content:
            text = getattr(tech_res.content[0], "text", str(tech_res.content[0]))
            for line in str(text).splitlines()[:6]:
                print("  ", line)
        else:
            print("  ", str(tech_res)[:200])

        return True
    except Exception as e:
        print(f"[FAIL] Momentum MCP test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    td_ok = test_tdpro()
    wb_ok = await test_webull_mcp()
    tt_ok = await test_tickertrace_mcp()
    mom_ok = await test_momentum_mcp()
    print("\n" + "=" * 60)
    print(f"SUMMARY: TDPro: {'PASSED' if td_ok else 'FAILED'} | Webull: {'PASSED' if wb_ok else 'FAILED'} | TickerTrace: {'PASSED' if tt_ok else 'FAILED'} | Momentum: {'PASSED' if mom_ok else 'FAILED'}")
    print("=" * 60)
    if not (td_ok and wb_ok and tt_ok and mom_ok):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
