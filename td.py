"""TraderDaddy Pro client.

TDPro's only public surface is a stateless MCP endpoint, but it accepts a bare
`tools/call` with no initialize handshake — so it's just one JSON-RPC POST. No
MCP library needed. Degrades to `configured: False` when TD_API_KEY is absent.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

BASE_URL = os.environ.get("TD_BASE_URL", "https://api.traderdaddy.pro")
MCP_PATH = "/api/v1/mcp"
TIMEOUT = 30
CACHE_TTL_SEC = 120.0  # signals are slow-moving; be kind to the rate limit

RATE_LIMIT_CODE = -32000


class TDProError(RuntimeError):
    pass


class TDPro:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("TD_API_KEY", "")
        self._cache: dict[str, tuple[float, Any]] = {}
        self._id = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        # The SDK sends both; mirror it rather than guess which one the gate reads.
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-API-Key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }

    def call(self, tool: str, args: dict | None = None) -> Any:
        if not self.configured:
            raise TDProError("TD_API_KEY not set")
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                "params": {"name": tool, "arguments": args or {}}}
        r = requests.post(BASE_URL + MCP_PATH, headers=self._headers(), json=body, timeout=TIMEOUT)

        if r.status_code == 429:
            raise TDProError(f"rate limited (retry after {r.headers.get('Retry-After', '?')}s)")
        r.raise_for_status()

        # TDPro returns UTF-8 but doesn't always declare a charset, so requests
        # falls back to ISO-8859-1 and em-dashes arrive as "â€"". Pin it.
        r.encoding = "utf-8"

        payload = _decode(r)
        if "error" in payload:
            err = payload["error"]
            if err.get("code") == RATE_LIMIT_CODE:
                raise TDProError("rate limited")
            raise TDProError(err.get("message", "unknown JSON-RPC error"))

        # The real payload is JSON wrapped in a text content block.
        content = (payload.get("result") or {}).get("content") or []
        if not content:
            return payload.get("result")
        text = content[0].get("text", "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def cached(self, tool: str, args: dict | None = None, ttl: float = CACHE_TTL_SEC) -> Any:
        key = tool + json.dumps(args or {}, sort_keys=True)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = self.call(tool, args)
        self._cache[key] = (now, val)
        return val

    def snapshot(self, symbols: list[str] | None = None) -> dict:
        """Market context for the header strip. Never raises — reports per-tool errors."""
        if not self.configured:
            return {"configured": False, "reason": "TD_API_KEY not set"}
        out: dict[str, Any] = {"configured": True, "fetched_at": time.time()}
        for label, tool in (("market_health", "get_market_health"), ("market_stats", "get_market_stats")):
            try:
                out[label] = self.cached(tool)
            except Exception as e:
                out[label] = {"error": str(e)}
        # Market-wide gauge: omit `symbol` entirely. Passing an unknown key (e.g.
        # `ticker`) is silently ignored and yields this same market-wide payload —
        # which is how five "per-ticker" calls all returned an identical score.
        try:
            out["conviction_market"] = self.cached("get_conviction")
        except Exception as e:
            out["conviction_market"] = {"error": str(e)}

        if symbols:
            conv: dict[str, Any] = {}
            for s in symbols[:6]:  # bound the fan-out against the rate limit
                try:
                    conv[s] = self.cached("get_conviction", {"symbol": s})
                except Exception as e:
                    conv[s] = {"error": str(e)}
            out["conviction"] = conv
        return out


def _decode(r: requests.Response) -> dict:
    """Handle both application/json and text/event-stream responses."""
    ctype = r.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        last = ""
        for line in r.text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    last = chunk
        if not last:
            raise TDProError("empty SSE stream")
        return json.loads(last)
    # Decode explicitly rather than trust r.json()'s charset inference.
    return json.loads(r.content.decode("utf-8"))
