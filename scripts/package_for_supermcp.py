#!/usr/bin/env python3
"""
package_for_supermcp.py

Packages the Momentum MCP tool suite into a clean, standalone bundle ready
to be deployed to supermcp on Coolify (Vultr).

Creates:
- dist/supermcp_tools/ (ready-to-copy Python package)
- dist/supermcp_momentum_tools.tar.gz (tarball for quick SCP / transfer)
- dist/supermcp_requirements.txt (clean minimal deps for remote host)
- dist/README_SUPERMCP.md (step-by-step instructions for integration into app.py)
"""

import os
import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
TARGET_PKG = DIST / "supermcp_tools" / "mcp_server"


def build_package():
    print("📦 Packaging Momentum MCP tools for supermcp...")
    
    # 1. Clean & recreate dist directory
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True, exist_ok=True)
    TARGET_PKG.mkdir(parents=True, exist_ok=True)

    # 2. Copy all mcp_server source files
    src_dir = ROOT / "mcp_server"
    for item in src_dir.iterdir():
        if item.name == "__pycache__":
            continue
        if item.is_dir():
            shutil.copytree(item, TARGET_PKG / item.name)
        else:
            shutil.copy2(item, TARGET_PKG / item.name)
    
    print(f"  ✓ Copied mcp_server files to {TARGET_PKG}")

    # 3. Create requirements file for remote supermcp
    reqs = """# Momentum MCP Tool Suite Requirements for supermcp
# Add these to supermcp's requirements.txt on Coolify / Vultr

fastmcp>=3.0
tradingview-screener>=3.0
tradingview-ta>=3.3
yfinance>=0.2
pandas>=2.0
pandas-ta>=0.4.0b0
mplfinance>=0.12.0a0
matplotlib>=3.7
feedparser>=6.0
trafilatura>=2.0
scipy>=1.10
python-dotenv>=1.0
requests
"""
    reqs_path = DIST / "supermcp_requirements.txt"
    reqs_path.write_text(reqs)
    print(f"  ✓ Created {reqs_path}")

    # 4. Create Integration Guide README
    readme = """# Momentum Tools Integration for supermcp

## 1. Copy Files to supermcp
On `ssh coolify` (in `~/supermcp` or the repo root):

```bash
# Option A: Extract tarball
tar -xzf supermcp_momentum_tools.tar.gz -C src/

# Option B: Direct directory copy
cp -r supermcp_tools/mcp_server src/
```

## 2. Install Dependencies
```bash
pip install -r supermcp_requirements.txt
```

## 3. Register Tools in `src/app.py`
In `src/app.py`, simply add:

```python
from src.mcp_server.registry import register_momentum_tools

# Register all 47 quantitative & research tools (Tiers 1, 2, & 3)
# onto supermcp's existing FastMCP server instance:
register_momentum_tools(mcp)

# OR register selectively by tier:
# register_momentum_tools(mcp, include_tiers=(1,))     # Tier 1 only (Pure REST / Flow / Sizing / SEC)
# register_momentum_tools(mcp, include_tiers=(1, 2))  # Tier 1 + 2 (Regime, Breadth, Screeners)
# register_momentum_tools(mcp, include_tiers=(1, 2, 3)) # All including VoPR options engine
```

## 4. Environment Variables
Ensure these keys exist in `supermcp/.env`:
- `TDPRO_API_KEY` (or `TD_API_KEY`) for live flow, GEX, and sentiment
- `SEC_USER_AGENT` (e.g. `MomentumAdmin admin@mphinance.com`) for EDGAR queries
"""
    readme_path = DIST / "README_SUPERMCP.md"
    readme_path.write_text(readme)
    print(f"  ✓ Created {readme_path}")

    # 5. Create tarball
    tar_path = DIST / "supermcp_momentum_tools.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(DIST / "supermcp_tools", arcname=".")
        tar.add(reqs_path, arcname="supermcp_requirements.txt")
        tar.add(readme_path, arcname="README_SUPERMCP.md")
    print(f"  ✓ Created tarball: {tar_path}")

    print("\n🚀 Done! Packaging complete. Files located in dist/")


if __name__ == "__main__":
    build_package()
