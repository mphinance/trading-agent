"""trading_mcp.bar_summary: pure bar structure analysis for voice co-pilot.

M8-02. Analyzes 5-minute OHLCV bars and renders the price structure and
volume relationship into speakable English phrases (e.g. "third consecutive
higher low, volume half the 20-bar average") along with small numeric facts.

Pure function, zero chart, image, or graphic rendering dependencies,
never emits raw bar dumps.
"""

from __future__ import annotations

from typing import Any, Mapping

_ORDINALS = {
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
}


class BarSummary(dict):
    """Dictionary holding bar structure facts with voice-friendly accessors."""

    @property
    def phrase(self) -> str:
        return self.get("phrase", "")

    def __str__(self) -> str:
        return self.phrase


def _num(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _get_val(bar: Any, *keys: str) -> float:
    if isinstance(bar, Mapping):
        for k in keys:
            if k in bar and bar[k] is not None:
                return _num(bar[k])
    else:
        for k in keys:
            if hasattr(bar, k):
                val = getattr(bar, k)
                if val is not None:
                    return _num(val)
    return 0.0


def summarize_bars_for_voice(bars: list[Any] | None) -> BarSummary:
    """Analyze OHLCV bar series and produce a speakable summary phrase and numeric facts.

    Args:
        bars: Chronological list of bars (oldest to newest), where each bar
              provides low, high, close, and volume.

    Returns:
        BarSummary dict containing 'phrase' and derived numeric facts.
        Never contains the raw bar list.
    """
    if not bars:
        return BarSummary({
            "phrase": "no bar data available",
            "consecutive_higher_lows": 0,
            "consecutive_lower_lows": 0,
            "consecutive_higher_highs": 0,
            "consecutive_lower_highs": 0,
            "volume_ratio": 0.0,
            "range_direction": "unknown",
        })

    if len(bars) == 1:
        cur_vol = _get_val(bars[0], "volume", "v", "vol")
        return BarSummary({
            "phrase": "insufficient bar history, 1 bar available",
            "consecutive_higher_lows": 0,
            "consecutive_lower_lows": 0,
            "consecutive_higher_highs": 0,
            "consecutive_lower_highs": 0,
            "volume_ratio": 1.0,
            "range_direction": "flat",
            "current_close": _get_val(bars[0], "close", "c", "last", "last_price"),
            "current_low": _get_val(bars[0], "low", "l"),
            "current_high": _get_val(bars[0], "high", "h"),
            "current_volume": cur_vol,
        })

    lows = [_get_val(b, "low", "l") for b in bars]
    highs = [_get_val(b, "high", "h") for b in bars]
    closes = [_get_val(b, "close", "c", "last", "last_price") for b in bars]
    volumes = [_get_val(b, "volume", "v", "vol") for b in bars]

    # Count consecutive higher / lower lows leading into the latest bar (bars[-1])
    higher_lows = 0
    lower_lows = 0
    for i in range(len(lows) - 1, 0, -1):
        if lows[i] > lows[i - 1]:
            if lower_lows == 0:
                higher_lows += 1
            else:
                break
        elif lows[i] < lows[i - 1]:
            if higher_lows == 0:
                lower_lows += 1
            else:
                break
        else:
            break

    # Count consecutive higher / lower highs leading into latest bar
    higher_highs = 0
    lower_highs = 0
    for i in range(len(highs) - 1, 0, -1):
        if highs[i] > highs[i - 1]:
            if lower_highs == 0:
                higher_highs += 1
            else:
                break
        elif highs[i] < highs[i - 1]:
            if higher_highs == 0:
                lower_highs += 1
            else:
                break
        else:
            break

    # Range direction over available window (up to last 20 bars)
    window = min(len(closes), 20)
    net_change = closes[-1] - closes[-window]
    if net_change > 0.001:
        range_dir = "upward"
    elif net_change < -0.001:
        range_dir = "downward"
    else:
        range_dir = "consolidating"

    # Volume vs 20-bar average
    # Prior bars up to 20 preceding the current bar
    prior_vols = volumes[-21:-1] if len(volumes) > 20 else volumes[:-1]
    if prior_vols and sum(prior_vols) > 0:
        avg_vol = sum(prior_vols) / len(prior_vols)
    else:
        avg_vol = sum(volumes) / len(volumes)

    cur_vol = volumes[-1]
    vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0

    # Format structure phrase
    if higher_lows >= 2:
        ordinal = _ORDINALS.get(higher_lows, f"{higher_lows}th")
        struct_text = f"{ordinal} consecutive higher low"
    elif lower_lows >= 2:
        ordinal = _ORDINALS.get(lower_lows, f"{lower_lows}th")
        struct_text = f"{ordinal} consecutive lower low"
    else:
        struct_text = f"range direction {range_dir}"

    # Format volume phrase
    if 0.45 <= vol_ratio <= 0.55:
        vol_text = "volume half the 20-bar average"
    elif 0.90 <= vol_ratio <= 1.10:
        vol_text = "volume in line with the 20-bar average"
    elif 1.40 <= vol_ratio <= 1.60:
        vol_text = "volume 1.5x the 20-bar average"
    elif 1.90 <= vol_ratio <= 2.10:
        vol_text = "volume double the 20-bar average"
    else:
        vol_text = f"volume {vol_ratio:.1f}x the 20-bar average"

    phrase = f"{struct_text}, {vol_text}"

    return BarSummary({
        "phrase": phrase,
        "consecutive_higher_lows": higher_lows,
        "consecutive_lower_lows": lower_lows,
        "consecutive_higher_highs": higher_highs,
        "consecutive_lower_highs": lower_highs,
        "volume_ratio": round(vol_ratio, 2),
        "range_direction": range_dir,
        "current_close": closes[-1],
        "current_low": lows[-1],
        "current_high": highs[-1],
        "current_volume": cur_vol,
        "avg_volume_20": round(avg_vol, 2),
    })
