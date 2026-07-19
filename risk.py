"""Portfolio guardrails.

Deliberately opinionated and tuned for a small account, where fixed costs and
concentration hurt far more than they do at size. Each check returns a level
(ok | caution | alert) plus a plain-English message.
"""

from __future__ import annotations

# Tickers that are really the same bet. Leveraged/derivative ETFs track a single
# underlying, so holding both reads as diversification while being one position.
# Extend as needed: {ticker: underlying}
RELATED: dict[str, str] = {
    "ONDL": "ONDS",  # 2x leveraged ETF tracking Ondas Holdings
    "ONDS": "ONDS",
}

CONCENTRATION_ALERT = 0.35  # single position > 35% of NLV
CONCENTRATION_CAUTION = 0.25
DRAWDOWN_ALERT = -0.25  # portfolio down > 25% on cost
DRAWDOWN_CAUTION = -0.10
LOW_BP_RATIO = 0.10  # buying power < 10% of NLV


def evaluate(portfolio: dict) -> list[dict]:
    checks: list[dict] = []
    totals = portfolio["totals"]
    positions = portfolio["positions"]
    nlv = totals["nlv"] or 1.0

    checks.append(_concentration(positions, nlv))
    checks.append(_correlated(positions, nlv))
    checks.append(_drawdown(totals))
    checks.append(_buying_power(totals, nlv))
    checks.append(_breadth(totals))
    return [c for c in checks if c]


def _mk(name: str, level: str, message: str, detail: str = "", meter: dict | None = None) -> dict:
    return {"name": name, "level": level, "message": message, "detail": detail, "meter": meter}


def _meter(value: float, limit: float, max_: float, caption: str, label_limit: str) -> dict:
    """A ratio against the limit that would trip this check.

    value/limit/max share one unit so the UI can draw a fill and a limit tick on
    the same track — the tick is what makes the number mean something.
    """
    return {"value": value, "limit": limit, "max": max_, "caption": caption, "label_limit": label_limit}


def _concentration(positions: list[dict], nlv: float) -> dict:
    if not positions:
        return _mk("Concentration", "ok", "No positions")
    top = max(positions, key=lambda p: p["market_value"])
    share = top["market_value"] / nlv
    detail = f"{top['symbol']} is {share:.0%} of NLV (${top['market_value']:,.2f})"
    meter = _meter(share, CONCENTRATION_ALERT, 0.60,
                   f"{top['symbol']} {share:.0%} · alert at {CONCENTRATION_ALERT:.0%}", f"{CONCENTRATION_ALERT:.0%}")
    if share >= CONCENTRATION_ALERT:
        return _mk("Concentration", "alert", f"{share:.0%}", detail, meter)
    if share >= CONCENTRATION_CAUTION:
        return _mk("Concentration", "caution", f"{share:.0%}", detail, meter)
    return _mk("Concentration", "ok", f"{share:.0%}", detail, meter)


def _correlated(positions: list[dict], nlv: float) -> dict:
    groups: dict[str, list[dict]] = {}
    for p in positions:
        underlying = RELATED.get(p["symbol"])
        if underlying:
            groups.setdefault(underlying, []).append(p)

    for underlying, members in groups.items():
        if len(members) > 1:
            syms = " + ".join(sorted(m["symbol"] for m in members))
            mv = sum(m["market_value"] for m in members)
            pl = sum(m["unrealized_pl"] for m in members)
            return _mk(
                "Correlated exposure",
                "alert",
                f"{mv / nlv:.0%}",
                f"{syms} both track {underlying}: ${mv:,.2f} combined, ${pl:,.2f} unrealized. "
                f"A move in {underlying} hits both at once — this is one bet, not two.",
                _meter(mv / nlv, CONCENTRATION_ALERT, 0.60,
                       f"{syms} combined · alert at {CONCENTRATION_ALERT:.0%}", f"{CONCENTRATION_ALERT:.0%}"),
            )
    return _mk("Correlated exposure", "ok", "none", "No overlapping underlyings detected")


def _drawdown(totals: dict) -> dict:
    pct = totals["unrealized_pl_pct"]
    pl = totals["unrealized_pl"]
    detail = f"${pl:,.2f} unrealized on ${totals['cost']:,.2f} cost basis"
    meter = _meter(min(abs(pct), 0.60), abs(DRAWDOWN_ALERT), 0.60,
                   f"{abs(pct):.0%} underwater · alert at {abs(DRAWDOWN_ALERT):.0%}", f"{abs(DRAWDOWN_ALERT):.0%}")
    if pct <= DRAWDOWN_ALERT:
        return _mk("Drawdown", "alert", f"{pct:.0%}", detail, meter)
    if pct <= DRAWDOWN_CAUTION:
        return _mk("Drawdown", "caution", f"{pct:.0%}", detail, meter)
    return _mk("Drawdown", "ok", f"{pct:+.0%}", detail, meter)


def _buying_power(totals: dict, nlv: float) -> dict:
    bp = totals["buying_power"]
    ratio = bp / nlv
    detail = f"${bp:,.2f} available against ${nlv:,.2f} NLV — shared across all accounts"
    meter = _meter(ratio, LOW_BP_RATIO, 0.50,
                   f"{ratio:.0%} deployable · thin below {LOW_BP_RATIO:.0%}", f"{LOW_BP_RATIO:.0%}")
    if ratio < LOW_BP_RATIO:
        return _mk("Dry powder", "caution", f"{ratio:.0%}", detail, meter)
    return _mk("Dry powder", "ok", f"{ratio:.0%}", detail, meter)


def _breadth(totals: dict) -> dict:
    n, w, losers = totals["position_count"], totals["winners"], totals["losers"]
    if not n:
        return _mk("Breadth", "ok", "—", "No positions")
    meter = _meter(w, max(1, n) * 0.5, max(1, n), f"{w} of {n} green", "half")
    if losers == n:
        return _mk("Breadth", "alert", f"0/{n}",
                   "Nothing is working. This is a signal about entry timing or selection, not any one ticker.",
                   meter)
    return _mk("Breadth", "ok", f"{w}/{n}", f"{w} of {n} positions green", meter)
