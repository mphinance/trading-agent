"""Reprice one option contract at a different spot price than its current quote.

The motivating case: a stock gaps premarket and there is no options market yet
to just read a new price off of. This carries forward the contract's real
quoted IV (or a realized-vol fallback when that quote is stale/illiquid) and
reprices via the existing Black-Scholes engine in `core/options_greeks.py` at
whatever spot the caller supplies -- typically a premarket price.

Not a forecast: it answers "what would this contract be worth if the stock is
at $X and IV is Y%", not "what will it actually print." The IV assumption is
the whole ballgame, which is why the output always includes a sensitivity
table rather than a single number.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from core.data import get_historical_data, get_live_price
from core.options_greeks import (
    bs_call_price,
    bs_delta,
    bs_gamma,
    bs_put_price,
    bs_theta,
    bs_vega,
    composite_rv,
)

logger = logging.getLogger(__name__)

# A contract that hasn't traded in longer than this, or shows no bid/ask at
# all, is treated as having no live market -- its own impliedVolatility field
# is then untrustworthy (can read e.g. 12% on a 70%-realized-vol name).
_STALE_QUOTE_MAX_AGE_DAYS = 2


def _round(value: float | None, ndigits: int = 4) -> float | None:
    return round(value, ndigits) if value is not None else None


def _to_ohlcv_df(records: list[dict]) -> pd.DataFrame:
    """Same shape core/options.py's analyze_options_setup builds -- local copy
    rather than importing a module-private helper across files."""
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}, inplace=True)
    for col in ["Open", "High", "Low", "Close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    return df


def _fetch_chain_row(
    ticker: str, expiration: str, strike: float, option_type: str
) -> dict[str, Any] | None:
    """Blocking -- wrap in asyncio.to_thread. Real per-contract quote from yfinance."""
    try:
        chain = yf.Ticker(ticker).option_chain(expiration)
    except Exception as exc:
        logger.warning("option_chain fetch failed for %s %s: %s", ticker, expiration, exc)
        return None

    table = chain.puts if option_type.lower() == "put" else chain.calls
    matches = table[np.isclose(table["strike"].astype(float), float(strike))]
    if matches.empty:
        return None
    row = matches.iloc[0]

    last_trade_ts = row.get("lastTradeDate")
    age_days = None
    if pd.notna(last_trade_ts):
        try:
            if last_trade_ts.tzinfo is not None:
                age_days = (pd.Timestamp.now(tz="UTC") - last_trade_ts.tz_convert("UTC")).days
            else:
                age_days = (pd.Timestamp.now() - last_trade_ts).days
        except Exception:
            age_days = None

    def _num(key: str) -> float | None:
        v = row.get(key)
        return float(v) if pd.notna(v) else None

    return {
        "contract_symbol": row.get("contractSymbol"),
        "last_price": _num("lastPrice"),
        "bid": _num("bid") or 0.0,
        "ask": _num("ask") or 0.0,
        "implied_vol": _num("impliedVolatility"),
        "last_trade_date": last_trade_ts.isoformat() if pd.notna(last_trade_ts) else None,
        "last_trade_age_days": age_days,
        "volume": _num("volume"),
        "open_interest": _num("openInterest"),
    }


def _quote_is_stale(quote: dict[str, Any] | None) -> tuple[bool, str | None]:
    if quote is None:
        return True, "no matching contract found in the chain"
    if not quote["bid"] and not quote["ask"]:
        return True, "bid and ask are both 0 -- no live market on this contract"
    age = quote.get("last_trade_age_days")
    if age is not None and age > _STALE_QUOTE_MAX_AGE_DAYS:
        return True, f"last traded {age}d ago"
    return False, None


async def reprice_option(
    ticker: str,
    strike: float,
    expiration: str,
    option_type: str = "call",
    target_spot: float | None = None,
    iv_override: float | None = None,
    contracts: int = 1,
    risk_free_rate: float = 0.05,
) -> dict[str, Any]:
    """Theoretical price (and greeks) for one contract at a chosen spot price.

    Args:
        ticker: Underlying symbol.
        strike: Strike price.
        expiration: Expiration date, "YYYY-MM-DD".
        option_type: "call" or "put".
        target_spot: Spot to reprice at (e.g. a premarket price). Defaults to
            the current live price when omitted.
        iv_override: Volatility as a fraction (0.85 = 85%). Takes precedence
            over the market-quoted IV and the realized-vol fallback.
        contracts: Number of contracts, for the dollar total.
        risk_free_rate: Annualized, as a fraction.

    Returns a dict; never raises. Missing/unusable inputs come back as an
    ``"error"`` key rather than a wrong number.
    """
    ticker = ticker.strip().upper()
    is_put = option_type.lower() == "put"
    strike = float(strike)

    try:
        exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    except ValueError:
        return {"error": f"expiration must be 'YYYY-MM-DD', got {expiration!r}"}

    dte = (exp_date - date.today()).days
    if dte < 0:
        return {"error": f"{expiration} is in the past"}
    T = max(dte / 365.0, 1 / 365.0)

    quote = await asyncio.to_thread(_fetch_chain_row, ticker, expiration, strike, option_type)
    stale, stale_reason = _quote_is_stale(quote)

    rv = None
    try:
        records = await get_historical_data(ticker, period="3mo", interval="1d")
        if len(records) >= 20:
            df = await asyncio.to_thread(_to_ohlcv_df, records)
            rv = composite_rv(df)
    except Exception as exc:
        logger.warning("realized-vol fetch failed for %s: %s", ticker, exc)

    if iv_override is not None:
        iv_used, iv_source = float(iv_override), "override"
    elif quote and quote.get("implied_vol") and not stale:
        iv_used, iv_source = float(quote["implied_vol"]), "market_quote"
    elif rv:
        iv_used, iv_source = rv * 1.3, "realized_vol_fallback"
    else:
        return {
            "error": (
                f"No usable IV for {ticker}: market quote is "
                f"{'stale (' + stale_reason + ')' if quote else 'missing'} "
                "and realized vol could not be computed. Pass iv_override."
            ),
            "stale_quote": stale,
            "stale_reason": stale_reason,
        }

    current_spot: float | None = None
    try:
        current_spot = await get_live_price(ticker)
    except Exception as exc:
        logger.warning("get_live_price failed for %s: %s", ticker, exc)

    if target_spot is not None:
        spot_used = float(target_spot)
    elif current_spot is not None:
        spot_used = current_spot
    else:
        return {"error": f"No target_spot given and get_live_price failed for {ticker}."}

    price_fn = bs_put_price if is_put else bs_call_price

    def _greeks_at(spot: float, sigma: float) -> dict[str, float | None]:
        price = price_fn(spot, strike, T, risk_free_rate, sigma)
        return {
            "price": _round(price),
            "delta": _round(bs_delta(spot, strike, T, risk_free_rate, sigma, is_put=is_put)),
            "gamma": _round(bs_gamma(spot, strike, T, risk_free_rate, sigma)),
            "theta_per_day": _round(bs_theta(spot, strike, T, risk_free_rate, sigma, is_put=is_put)),
            "vega_per_vol_point": _round(bs_vega(spot, strike, T, risk_free_rate, sigma)),
        }

    theo = _greeks_at(spot_used, iv_used)
    contracts = max(int(contracts), 1)
    total_cost_est = _round(theo["price"] * 100 * contracts, 2) if theo["price"] is not None else None

    # Calibration: same model, same IV, but at the CURRENT live spot (not the
    # hypothetical target_spot) vs. what this contract actually last traded
    # at. Tells you whether the IV assumption is roughly sane before trusting
    # it at a different, hypothetical spot -- it is not a claim about what
    # the underlying was doing back when that trade printed.
    calibration = None
    if quote and quote.get("last_price") is not None and current_spot is not None:
        calibration = {
            "current_spot": current_spot,
            "theo_price_at_current_spot": _greeks_at(current_spot, iv_used)["price"],
            "last_traded_option_price": quote["last_price"],
        }

    sensitivity = [
        {"iv_pct": _round(iv_used * 100, 1), **_greeks_at(spot_used, iv_used)},
        {"iv_pct": _round(iv_used * 0.85 * 100, 1), **_greeks_at(spot_used, iv_used * 0.85)},
        {"iv_pct": _round(iv_used * 1.15 * 100, 1), **_greeks_at(spot_used, iv_used * 1.15)},
    ]

    return {
        "ticker": ticker,
        "strike": strike,
        "expiration": expiration,
        "option_type": "put" if is_put else "call",
        "dte": dte,
        "spot_used": spot_used,
        "contracts": contracts,
        "iv_used_pct": _round(iv_used * 100, 2),
        "iv_source": iv_source,
        "stale_quote": stale,
        "stale_reason": stale_reason,
        "market_quote": quote,
        "theoretical": theo,
        "total_cost_est": total_cost_est,
        "calibration": calibration,
        "iv_sensitivity": sensitivity,
    }
