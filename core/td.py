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

# Dealer gamma moves slower still, and the upstream itself only recomputes
# indices every ~5-15 min. Non-index names are computed on demand (~2-4s on a
# cold call), so a short TTL here would make every glance pay that cost.
GEX_TTL_SEC = 300.0

# Strikes to keep when compacting the ladder for a prompt. get_gex_ticker
# returns the FULL chain — ~200 strikes, ~40KB of JSON for SPY. That is fine
# for a chart and ruinous for a chat turn, which is why nothing injects the raw
# payload; see levels().
WALL_COUNT = 6
WALL_RANGE = 0.05  # only strikes within +/-5% of spot can act as a near wall

RATE_LIMIT_CODE = -32000


from pathlib import Path

LOCAL_ENV = Path(__file__).resolve().parent / ".env"
PARENT_ENV = Path(__file__).resolve().parent.parent / ".env.webull"


def _get_api_key() -> str:
    key = os.environ.get("TD_API_KEY") or os.environ.get("TDPRO_API_KEY")
    if key:
        return key
    for env_path in (LOCAL_ENV, PARENT_ENV):
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k in ("TD_API_KEY", "TDPRO_API_KEY"):
                        return v.strip().strip('"').strip("'")
    return ""


class TDProError(RuntimeError):
    pass


class TDPro:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or _get_api_key()
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

    def levels(self, symbol: str) -> dict:
        """Dealer-gamma structure for one symbol, compacted for display and prompts.

        Merges two tools that describe the same structure with different names
        and different maths:

          get_gex_ticker  -> `gammaFlipLevel`, `maxGammaStrike`, the raw per-strike
                             ladder, and a prose `interpretation`.
          get_apex_levels -> `gammaFlip` plus strikes SCORED 0-100 by open-interest
                             mass blended with net gamma.

        The two flip levels are computed differently and can genuinely disagree,
        which matters because the flip is a regime boundary: on opposite sides of
        it the same tape reads as dampened or amplified. So both are reported and
        a disagreement is flagged rather than silently resolved. Apex is preferred
        when present (it simulates the crossing rather than reading the sign
        change off the ladder), matching how the walls are drawn elsewhere.

        Apex is a premium tool. When it is gated the call fails and this degrades
        to the gex-only picture rather than reporting nothing.

        Never raises — returns {"error": ...} so a dead signal can't take down a
        chat turn or the portfolio poll.
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return {"error": "no symbol"}
        if not self.configured:
            return {"symbol": symbol, "error": "TD_API_KEY not set"}

        try:
            g = self.cached("get_gex_ticker", {"symbol": symbol}, ttl=GEX_TTL_SEC)
        except Exception as e:
            return {"symbol": symbol, "error": str(e)}
        if not isinstance(g, dict):
            return {"symbol": symbol, "error": "unexpected GEX payload"}

        apex: dict | None = None
        apex_note = None
        try:
            a = self.cached("get_apex_levels", {"symbol": symbol}, ttl=GEX_TTL_SEC)
            if isinstance(a, dict) and a.get("levels"):
                apex = a
            else:
                apex_note = "apex returned no levels"
        except Exception as e:
            apex_note = f"apex unavailable ({e})"

        spot = _num(g.get("spotPrice"))
        flip_gex = _num(g.get("gammaFlipLevel"))
        flip_apex = _num(apex.get("gammaFlip")) if apex else None
        flip = flip_apex if flip_apex else flip_gex
        interp = g.get("interpretation") or {}

        out: dict[str, Any] = {
            "symbol": symbol,
            "spot": spot,
            "regime": interp.get("marketRegime"),
            "price_action": interp.get("priceAction"),
            "flip": flip,
            "flip_source": "apex" if flip_apex else "gex",
            "flip_gex": flip_gex,
            "flip_apex": flip_apex,
            # Both round to the same dollar on most days; only call it a split
            # when they'd actually put price on opposite sides of the boundary.
            "flip_split": bool(
                flip_apex and flip_gex and spot
                and (spot >= flip_apex) != (spot >= flip_gex)
            ),
            "pin": _num(g.get("maxGammaStrike")),
            "net_gex": _num(g.get("totalGEX")),
            "pc_gex_ratio": _num(g.get("putCallGEXRatio")),
            "key_levels": [
                {"strike": _num(k.get("strike")), "type": k.get("type"),
                 "net_gex": _num(k.get("netGex"))}
                for k in (g.get("keyLevels") or [])
            ],
            "walls": _walls(g.get("byStrike") or [], spot),
            "expirations": g.get("expirationsUsed") or [],
            "as_of": g.get("lastUpdated"),
        }
        if spot and flip:
            out["above_flip"] = spot >= flip
        if apex:
            out["apex"] = [
                {"strike": _num(l.get("strike")), "score": _num(l.get("score")),
                 "rank": l.get("rank"), "oi": _num(l.get("totalOI")),
                 "above_spot": l.get("isAboveSpot")}
                for l in (apex.get("levels") or [])[:WALL_COUNT]
            ]
        if apex_note:
            out["apex_note"] = apex_note
        # A gamma pocket only exists on non-index names and is a positioning
        # divergence from the broad regime, not a direction call — carry it
        # through when set so the read can mention it.
        if g.get("gammaPocket"):
            out["gamma_pocket"] = g["gammaPocket"]
        return out

    def get_market_health(self) -> dict[str, Any]:
        """Fetch composite market health score (0-7 scale)."""
        return self.cached("get_market_health", ttl=300.0) or {}


def build_levels_of():
    """Return a `levels_of(symbol) -> dict | None` backed by `TDPro.levels()`.

    Moved here (M0-06) from `vesper/alerts_runner.py`, which still owns the
    live alert watcher (`build_watcher()`) and imports this back from core —
    the same shape M0-02 already put every other pure read helper in. Before
    the move, `trading_mcp/vesper_tools.py`'s `list_alerts` tool reached this
    logic via `from vesper.alerts_runner import _build_levels_of`, which was
    the last `vesper.*` reference left in that file; it now imports this
    function directly and `vesper/alerts_runner.py`'s own `_build_levels_of`
    is a thin wrapper around it so `build_watcher()` is unchanged.

    Returns None (never a remembered/stale number) when TDPro is unconfigured
    or the call fails -- `alerts.resolve_level()` treats None as "cannot
    resolve" and leaves a dynamic alert pending rather than firing it against
    a stale level. That is the specific failure this helper exists to
    prevent, so do not add a fallback here.
    """
    client = TDPro()

    def levels_of(symbol: str):
        if not client.configured:
            return None
        try:
            data = client.levels(symbol)
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("error"):
            return None
        return data

    return levels_of


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _walls(by_strike: list, spot: float | None) -> list[dict]:
    """The heaviest strikes near spot, by absolute net gamma.

    Ranking by |netGex| rather than by gamma alone keeps put walls (negative)
    in the list — they are where the hedging sits on the way down, and dropping
    them would leave a read with resistance and no support.
    """
    if not spot:
        return []
    near = []
    for s in by_strike:
        strike, net = _num(s.get("strike")), _num(s.get("netGex"))
        if strike is None or not net:
            continue
        if abs(strike - spot) / spot > WALL_RANGE:
            continue
        near.append({
            "strike": strike,
            "net_gex": net,
            "side": "above" if strike > spot else "below",
            "call_oi": _num(s.get("callOi")) or 0,
            "put_oi": _num(s.get("putOi")) or 0,
        })
    near.sort(key=lambda w: abs(w["net_gex"]), reverse=True)
    return sorted(near[:WALL_COUNT], key=lambda w: w["strike"])


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
