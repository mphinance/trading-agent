"""Options Order Flow Classification: Directional vs. Institutional/Dealer Hedge.

A pure, deterministic classifier scoring large options prints based on:
1. Volume-to-Open-Interest ratio (vsOI / trade_size vs open_interest)
2. Proximity to dealer gamma flip level (|distance_from_flip_pct|)
3. Implied Volatility and IV Rank (willingness to pay volatility premium)
4. Moneyness and option positioning

Degrades cleanly to AMBIGUOUS when signals are mixed or confidence is low,
satisfying the codebase's 'never fabricate confidence' invariant.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

FlowClassification = Literal["DIRECTIONAL", "HEDGE", "AMBIGUOUS"]


def classify_flow(
    trade_size: float,
    open_interest: float,
    iv: float,
    iv_rank: Optional[float] = None,
    distance_from_flip_pct: Optional[float] = None,
    option_type: str = "CALL",
    moneyness_pct: float = 0.0,
    sentiment: Optional[str] = None,
) -> FlowClassification:
    """Classify an options trade print as DIRECTIONAL, HEDGE, or AMBIGUOUS.

    Parameters:
        trade_size: Trade volume in number of contracts (e.g. 5,000).
        open_interest: Existing open interest at the strike (e.g. 1,500).
        iv: Implied volatility as a decimal (e.g. 0.45) or percentage.
        iv_rank: 0-100 IV Rank percentile if available, otherwise None.
        distance_from_flip_pct: Optional percentage distance between spot price and gamma flip
            ((spot - flip) / spot * 100.0, e.g. +2.5% or -0.2%). None if flip is unknown.
        option_type: "CALL" or "PUT".
        moneyness_pct: Percentage distance from spot ((strike - spot) / spot * 100.0).
        sentiment: Sentiment label from unusual activity feed (e.g. "Bullish", "Bearish").

    Returns:
        "DIRECTIONAL", "HEDGE", or "AMBIGUOUS".
    """
    if trade_size <= 0:
        return "AMBIGUOUS"

    # Normalize IV if given as percentage > 2.0
    norm_iv = iv / 100.0 if iv > 2.0 else iv

    # 1. Volume vs Open Interest Multiple
    oi_multiple = trade_size / max(1.0, open_interest)

    # 2. Proximity to Gamma Flip (if available)
    abs_flip_dist = abs(distance_from_flip_pct) if distance_from_flip_pct is not None else None

    # 3. Volatility willingness score
    is_high_vol = (iv_rank is not None and iv_rank >= 50.0) or (norm_iv >= 0.40)
    is_low_vol = (iv_rank is not None and iv_rank < 35.0) or (norm_iv < 0.20)

    # ── Hedge Rules (evaluated first when flip proximity indicates delta/gamma rebalancing) ──
    if abs_flip_dist is not None and abs_flip_dist <= 0.75:
        if is_low_vol or oi_multiple < 2.0 or abs(moneyness_pct) <= 1.0:
            return "HEDGE"

    if option_type.upper() == "PUT" and abs(moneyness_pct) <= 2.0 and is_low_vol:
        if abs_flip_dist is not None and abs_flip_dist <= 1.2:
            return "HEDGE"

    # ── Directional Rules ───────────────────────────────────────────────────
    # Case A: Gamma flip known and trade is far from flip
    if abs_flip_dist is not None and abs_flip_dist >= 1.0 and oi_multiple >= 1.5:
        if is_high_vol or abs(moneyness_pct) >= 1.5 or (sentiment and sentiment.lower() in ("bullish", "bearish")):
            return "DIRECTIONAL"

    # Case B: Gamma flip unknown (general equities) — strong volume multiple and sentiment/vol/moneyness
    if abs_flip_dist is None and oi_multiple >= 1.5:
        if is_high_vol or abs(moneyness_pct) >= 1.5 or (sentiment and sentiment.lower() in ("bullish", "bearish")):
            return "DIRECTIONAL"

    # Extreme size (>3x OI)
    if oi_multiple >= 3.0:
        if abs_flip_dist is None or abs_flip_dist >= 1.0:
            return "DIRECTIONAL"

    # ── Default Fallback ───────────────────────────────────────────────────
    return "AMBIGUOUS"


def classify_unusual_activity_record(
    record: Dict[str, Any],
    spot_price: Optional[float] = None,
    gamma_flip: Optional[float] = None,
    iv_rank: Optional[float] = None,
) -> FlowClassification:
    """Convenience wrapper extracting fields from TraderDaddy's get_unusual_activity JSON format.

    Confirmed TraderDaddy fields:
        - volume: int
        - openInterest: int
        - type: "CALL" | "PUT"
        - moneynessPct: float
        - sentiment: str
    """
    trade_size = float(record.get("volume") or record.get("trade_size") or 0.0)
    open_interest = float(record.get("openInterest") or record.get("open_interest") or 0.0)
    option_type = str(record.get("type") or "CALL").upper()
    moneyness_pct = float(record.get("moneynessPct") or 0.0) * 100.0
    sentiment = record.get("sentiment")

    # Estimate IV or default to neutral 0.25 if unsupplied in public feed row
    iv = float(record.get("iv") or 0.25)

    dist_from_flip: Optional[float] = None
    if spot_price and gamma_flip and spot_price > 0:
        dist_from_flip = ((spot_price - gamma_flip) / spot_price) * 100.0

    return classify_flow(
        trade_size=trade_size,
        open_interest=open_interest,
        iv=iv,
        iv_rank=iv_rank,
        distance_from_flip_pct=dist_from_flip,
        option_type=option_type,
        moneyness_pct=moneyness_pct,
        sentiment=sentiment,
    )
