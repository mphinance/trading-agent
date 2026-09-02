"""trading_mcp.gamma_summary: compacted dealer-gamma levels for voice co-pilot.

M8-03. Extracts and summarizes dealer gamma from core.td's compacted levels()
shape (never get_gex_ticker's raw ~40KB chain payload).

Key invariants:
1. flip_split is surfaced directly when flip sources straddle spot, never silently resolved.
2. The serialized section stays under 1.5KB.
3. Language never uses target-implying phrasing ('will move to', 'targeting') -- gamma
   reflects hedging concentration and dealer positioning, not price forecasts.
"""

from __future__ import annotations

import json
from typing import Any

from core.td import TDPro, build_levels_of


FORBIDDEN_TARGET_WORDS = (
    "will move to",
    "targeting",
    "target is",
    "will reach",
    "headed to",
    "expected to reach",
    "forecast",
)


def get_compact_gamma(symbol: str, client: TDPro | None = None) -> dict[str, Any]:
    """Fetch compacted dealer gamma for symbol using core.td.levels()."""
    if client is None:
        client = TDPro()
    if not client.configured:
        return {"available": False, "symbol": symbol, "reason": "TDPro unconfigured"}
    try:
        raw_levels = client.levels(symbol)
    except Exception as e:
        return {"available": False, "symbol": symbol, "reason": str(e)}

    return format_gamma_for_voice(raw_levels)


def format_gamma_for_voice(levels_data: dict[str, Any] | None) -> dict[str, Any]:
    """Format core.td.levels() output into a compact, speakable payload.

    Keeps serialized size under 1.5KB, preserves flip_split, and avoids target language.
    """
    if not levels_data or not isinstance(levels_data, dict) or levels_data.get("error"):
        return {
            "available": False,
            "reason": levels_data.get("error", "No gamma data") if isinstance(levels_data, dict) else "No gamma data",
        }

    symbol = levels_data.get("symbol", "")
    spot = levels_data.get("spot")
    flip = levels_data.get("flip")
    flip_split = bool(levels_data.get("flip_split", False))
    flip_apex = levels_data.get("flip_apex")
    flip_gex = levels_data.get("flip_gex")
    pin = levels_data.get("pin")
    regime = levels_data.get("regime") or "neutral"
    above_flip = levels_data.get("above_flip")

    # Filter and keep only nearest walls to stay compact (<1.5KB)
    raw_walls = levels_data.get("walls") or []
    compact_walls = [
        {
            "strike": w.get("strike"),
            "side": w.get("side"),
            "net_gex": w.get("net_gex"),
        }
        for w in raw_walls[:4]
    ]

    # Generate speakable summary phrasing without any target-implying language
    phrases = []
    if flip_split and flip_apex and flip_gex:
        phrases.append(
            f"gamma flip is split between {flip_apex:.2f} (Apex) and {flip_gex:.2f} (GEX) straddling spot at {spot:.2f}"
            if spot else f"gamma flip is split between {flip_apex:.2f} and {flip_gex:.2f}"
        )
    elif flip is not None:
        pos_rel = "above" if above_flip else "below"
        phrases.append(f"spot is {pos_rel} gamma flip at {flip:.2f}")

    if regime:
        regime_clean = regime.replace("_", " ")
        phrases.append(f"dealer regime is {regime_clean}")

    if pin is not None:
        phrases.append(f"heavy hedging concentration pinned at {pin:.2f}")

    summary_phrase = ", ".join(phrases) if phrases else "gamma levels neutral"

    # Enforce no target-implying language
    lower_phrase = summary_phrase.lower()
    for forbidden in FORBIDDEN_TARGET_WORDS:
        if forbidden in lower_phrase:
            raise ValueError(f"Target-implying phrase detected in gamma summary: {forbidden!r}")

    out = {
        "available": True,
        "symbol": symbol,
        "spot": spot,
        "regime": regime,
        "flip": flip,
        "flip_split": flip_split,
        "flip_gex": flip_gex,
        "flip_apex": flip_apex,
        "pin": pin,
        "above_flip": above_flip,
        "walls": compact_walls,
        "summary_phrase": summary_phrase,
    }

    # Verify payload size bound (< 1.5KB)
    payload_len = len(json.dumps(out))
    if payload_len > 1536:
        out["walls"] = compact_walls[:2]

    return out
