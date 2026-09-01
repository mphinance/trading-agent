# Remote `supermcp` Architecture & State

**Host:** `vultr` VPS (149.28.104.163) (SSH alias: `coolify`)
**Directory:** `~/supermcp`

## Lay of the Land

The `supermcp` environment serves as the unified dashboard and FastMCP server for the Momentum Phund. The system operates via `src/app.py` under `uvicorn` and is exposed behind Apache with OAuth 2.1 and static-token gating.

### Components & Merged Tooling
Unlike the original concept which heavily relied on Tastytrade and Substack, the current remote `supermcp` has **already absorbed significant portions of the Vesper/Webull-sidecar tools**, including:
1. **Brokers:** Tastytrade, Substack, Webull (via `src/brokers/webull.py`), ConnectTrade.
2. **Execution:** The real execution path exists in `src/orders.py` and is exposed via the `@mcp.tool` `place_live_order` in `src/app.py`.
3. **TraderDaddy Pro:** 11 flow, scan, and quant tools (`src/tdpro.py`).
4. **Scanners:** Gamma flush logic (`src/wallscan.py`) and screeners (`src/screener.py`).

### Execution Path Risk
In `src/app.py`, the `place_live_order` tool is routed through `orders.execute()`. While it requires the string `"SEND IT LIVE"` and a `LIVE_ORDERS_ENABLED` flag, **there is no secondary bash fencing or separate execution server**, and Webull acts as a direct execution path. This confirms the risks identified during Phase 0 of the consolidation plan: the Webull order flow is operating on a monolithic hub with all the heavy tools (RAG, quant, flow).

### The Packaging Target
Our built package (`dist/supermcp_momentum_tools.tar.gz`) is exactly what is needed here to finalize the consolidation:
- **Tier 1, 2, and 3 Tools** from our sidecar will drop perfectly into `src/app.py` via our new `mcp_server/registry.py`.
- **Decoupled execution:** If we strip the execution layers out of `supermcp`'s `app.py` into our planned Execution Engine, `supermcp` can become a pure Quant/Research Hub.

### Actions Taken
- Explored the remote `supermcp` structure over SSH (`coolify`).
- Updated the remote `CLAUDE.md` on the server to accurately reflect the presence of Webull, ConnectTrade, TDPro, and the execution risk paths, replacing the severely outdated documentation.
