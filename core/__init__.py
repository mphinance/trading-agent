"""core: quant analytics and data layer (Webull/TraderDaddy/EDGAR clients,
technicals, options, screeners, charts, macro regime/market-top detection).

No dependency on LangGraph or FastMCP -- vesper/ and mcp_server/ both import
downward into this package; it must never import either of them back.
"""

__version__ = "0.1.0"
