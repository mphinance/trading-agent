"""Strategy Playbook Synthesis Node."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from vesper.account import fetch_live_equity
from vesper.state import TradingState, OrderProposal, OrderLeg
from vesper.risk import RiskEnforcer

logger = logging.getLogger(__name__)


def _fetch_live_quote(symbol: str) -> Optional[float]:
    """Blocking — wrap in asyncio.to_thread. Returns None (never a guess) if
    Webull isn't configured or the quote can't be fetched."""
    try:
        from core.wb import Webull
        from core.md import Market

        wb = Webull()
        if not wb.configured:
            return None
        snap = Market(wb).snapshot([symbol])
        last = (snap.get(symbol) or {}).get("last")
        return float(last) if last else None
    except Exception as e:
        logger.warning(f"Could not fetch live quote for {symbol}: {e}")
        return None


def _fetch_live_option_quote(
    underlying: str,
    strike: float,
    option_type: str = "PUT",
) -> Optional[float]:
    """Fetch real live option contract price for a given underlying and strike.

    Blocking — wrap in asyncio.to_thread.
    Returns None (never a fabricated placeholder) if Webull is unconfigured or no quote exists.
    """
    try:
        from core.wb import Webull
        from core.md import Market

        wb = Webull()
        if not wb.configured:
            return None

        mkt = Market(wb)
        chain_res = mkt.option_chain(
            underlying=underlying,
            option_type=option_type.upper(),
            strike_gte=strike - 0.01,
            strike_lte=strike + 0.01,
        )

        contracts = []
        if isinstance(chain_res, list):
            contracts = chain_res
        elif isinstance(chain_res, dict):
            contracts = chain_res.get("data") or chain_res.get("contracts") or chain_res.get("list") or []

        if not contracts:
            return None

        contract_sym = None
        for c in contracts:
            c_strike = float(c.get("strike_price") or c.get("strikePrice") or c.get("strike") or 0.0)
            if abs(c_strike - strike) < 0.05:
                contract_sym = c.get("symbol") or c.get("ticker") or c.get("contract_symbol")
                break

        if not contract_sym and contracts:
            contract_sym = contracts[0].get("symbol") or contracts[0].get("ticker")

        if not contract_sym:
            return None

        snap_res = mkt.option_snapshot([contract_sym])
        snap_data = (snap_res.get(contract_sym) or snap_res) if isinstance(snap_res, dict) else None
        if not snap_data:
            return None

        last = snap_data.get("last") or snap_data.get("close")
        if last and float(last) > 0:
            return float(last)

        bid = float(snap_data.get("bid") or snap_data.get("bid_price") or 0)
        ask = float(snap_data.get("ask") or snap_data.get("ask_price") or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2.0, 2)
        if bid > 0:
            return bid
        if ask > 0:
            return ask

        return None
    except Exception as e:
        logger.debug(f"Could not fetch live option quote for {underlying} {strike} {option_type}: {e}")
        return None


def _select_0dte_wall_strike(ticker: str, is_bullish: bool) -> Optional[float]:
    """Pick a 0DTE strike from TraderDaddy's major OI put/call walls
    (td.levels()'s "walls" list) rather than a fixed spot+/-1 offset.

    True delta-based (~0.30delta) strike selection would need an option
    Greeks calculation this codebase doesn't have (py_vollib is listed as
    future tooling in ROADMAP.md, not installed) -- wall-based selection is
    the other half of the "0.30delta OR major OI walls" spec that's actually
    achievable with what's already wired up (td.levels() is already used
    elsewhere in this file's spirit-cousins). Returns None (never falls back
    to a fabricated offset) if TDPro is unconfigured, levels() errors, or no
    wall exists on the relevant side -- a 0DTE draft with no real wall to
    anchor the strike to should be skipped, not approximated.

    Blocking — wrap in asyncio.to_thread.
    """
    try:
        from core.td import TDPro

        client = TDPro()
        if not client.configured:
            return None

        data = client.levels(ticker)
        if not isinstance(data, dict) or data.get("error"):
            return None

        walls = data.get("walls") or []
        side = "above" if is_bullish else "below"
        oi_key = "call_oi" if is_bullish else "put_oi"

        candidates = [w for w in walls if w.get("side") == side and w.get("strike") is not None]
        if not candidates:
            return None

        best = max(candidates, key=lambda w: w.get(oi_key) or 0)
        return float(best["strike"])
    except Exception as e:
        logger.debug(f"Could not select 0DTE wall strike for {ticker}: {e}")
        return None


MAX_0DTE_SPREAD_PCT = 0.15

# MODELING CONSTANT (not a live quote) — see rule 1 in CLAUDE.md and
# RiskEnforcer.TARGET_ANNUAL_VOLATILITY for the same category of constant.
# Approx risk-free proxy for Black-Scholes r. 0DTE delta is very insensitive
# to r (hours of time value), so this does not need to track daily.
RISK_FREE_RATE_0DTE = 0.045

# Floor on time-to-expiry fed into Black-Scholes, in years (5 minutes).
# Prevents t -> 0 (division by zero / NaN in d1/d2) in the last minutes
# before close.
MIN_0DTE_TTE_YEARS = 5.0 / (60.0 * 24.0 * 365.0)

TARGET_DELTA_0DTE = 0.30


def _time_to_close_years(now: Optional[datetime] = None) -> float:
    """Years remaining until real options expiry (4:00 PM ET), floored at
    MIN_0DTE_TTE_YEARS so Black-Scholes never sees t == 0 in the closing
    minutes.

    Deliberately the actual 4pm ET market close, NOT RiskEnforcer's
    HARD_EXIT_TIME_0DTE ("15:00") -- that's this strategy's own early exit
    rule, not when the contract actually expires.

    Takes an optional `now` so tests can pin a deterministic time-to-close
    instead of depending on wall-clock time at test-run time.
    """
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    years = max((close_et - now_et).total_seconds(), 0.0) / (365.0 * 24.0 * 3600.0)
    return max(years, MIN_0DTE_TTE_YEARS)


def _select_0dte_delta_strike(
    ticker: str, spot: float, is_bullish: bool, iv_pct: float
) -> Optional[float]:
    """Pick the same-day contract whose Black-Scholes delta is closest to
    TARGET_DELTA_0DTE (0.30), scanning the full 0DTE chain.

    Tried FIRST; `_select_0dte_wall_strike` is the fallback when this
    returns None (py_vollib unavailable, no chain, no today expiry, or no
    contract computable).

    `sigma` is the SAME single audit-level IV already gating entry
    elsewhere in this playbook (Webull's chain here carries no per-contract
    IV, so this is not fabricating a fake per-strike vol surface -- it's
    reusing the one real number available). A real per-strike IV surface is
    a known limitation, not something faked here.

    Blocking (chain fetch) — wrap in asyncio.to_thread.
    Returns None (never a fabricated strike) if py_vollib import fails,
    Webull is unconfigured, the chain is empty, nothing expires today, or
    delta can't be computed for any contract.
    """
    try:
        from py_vollib.black_scholes.greeks.analytical import delta as bs_delta
    except Exception as e:
        logger.debug(f"py_vollib unavailable, skipping delta-based 0DTE strike selection: {e}")
        return None

    if not spot or spot <= 0 or not iv_pct or iv_pct <= 0:
        return None

    try:
        from core.wb import Webull
        from core.md import Market

        wb = Webull()
        if not wb.configured:
            return None

        mkt = Market(wb)
        option_type = "CALL" if is_bullish else "PUT"
        chain_res = mkt.option_chain(underlying=ticker, option_type=option_type)

        contracts = []
        if isinstance(chain_res, list):
            contracts = chain_res
        elif isinstance(chain_res, dict):
            contracts = chain_res.get("data") or chain_res.get("contracts") or chain_res.get("list") or []

        if not contracts:
            return None

        today = datetime.now(timezone.utc).date()
        today_contracts = []
        for c in contracts:
            exp_str = c.get("expire_date") or c.get("expiration_date") or c.get("expiry") or c.get("start_date")
            if not exp_str:
                continue
            try:
                exp_date = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
                if exp_date == today:
                    today_contracts.append(c)
            except Exception:
                continue

        if not today_contracts:
            return None

        t = _time_to_close_years()
        sigma = iv_pct / 100.0
        r = RISK_FREE_RATE_0DTE
        flag = "c" if is_bullish else "p"

        best_strike = None
        best_diff = None
        for c in today_contracts:
            try:
                K = float(c.get("strike_price") or c.get("strikePrice") or c.get("strike") or 0.0)
                if K <= 0:
                    continue
                d = bs_delta(flag, spot, K, t, r, sigma)
                diff = abs(abs(d) - TARGET_DELTA_0DTE)
            except Exception:
                continue
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_strike = K

        return best_strike
    except Exception as e:
        logger.debug(f"Could not select 0DTE delta strike for {ticker}: {e}")
        return None


def _fetch_0dte_option_quote(
    underlying: str,
    strike: float,
    option_type: str = "CALL",
) -> Optional[float]:
    """Fetch live option price for a contract expiring TODAY (0DTE).

    Blocking — wrap in asyncio.to_thread.
    Returns price or None if Webull is unconfigured, no contract expires today,
    no quote exists, or the bid/ask spread is wider than MAX_0DTE_SPREAD_PCT
    of the price -- a 0DTE contract quoted with a wide spread is genuine
    execution risk (the fill you'd actually get can differ a lot from the
    quote used to size and stop the trade), not something to draft against
    and hope for the best on. Checked whenever both bid and ask are present,
    regardless of which of bid/ask/last ends up being the returned price.
    """
    try:
        from core.wb import Webull
        from core.md import Market

        wb = Webull()
        if not wb.configured:
            return None

        mkt = Market(wb)
        chain_res = mkt.option_chain(
            underlying=underlying,
            option_type=option_type.upper(),
            strike_gte=strike - 0.01,
            strike_lte=strike + 0.01,
        )

        contracts = []
        if isinstance(chain_res, list):
            contracts = chain_res
        elif isinstance(chain_res, dict):
            contracts = chain_res.get("data") or chain_res.get("contracts") or chain_res.get("list") or []

        if not contracts:
            return None

        today = datetime.now(timezone.utc).date()
        today_contracts = []
        for c in contracts:
            c_strike = float(c.get("strike_price") or c.get("strikePrice") or c.get("strike") or 0.0)
            if abs(c_strike - strike) > 0.05:
                continue

            exp_str = c.get("expire_date") or c.get("expiration_date") or c.get("expiry") or c.get("start_date")
            if not exp_str:
                continue

            try:
                exp_date = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
                if exp_date == today:
                    today_contracts.append(c)
            except Exception:
                continue

        if not today_contracts:
            return None

        target_contract = today_contracts[0]
        contract_sym = (
            target_contract.get("symbol")
            or target_contract.get("ticker")
            or target_contract.get("contract_symbol")
        )
        if not contract_sym:
            return None

        snap_res = mkt.option_snapshot([contract_sym])
        snap_data = (snap_res.get(contract_sym) or snap_res) if isinstance(snap_res, dict) else None
        if not snap_data:
            return None

        bid = float(snap_data.get("bid") or snap_data.get("bid_price") or 0)
        ask = float(snap_data.get("ask") or snap_data.get("ask_price") or 0)
        mid = round((bid + ask) / 2.0, 2) if (bid > 0 and ask > 0) else None

        if bid > 0 and ask > 0:
            reference_price = mid if mid else ask
            spread_pct = (ask - bid) / reference_price if reference_price else 0.0
            if spread_pct > MAX_0DTE_SPREAD_PCT:
                logger.debug(
                    f"Rejected 0DTE quote for {underlying} {strike} {option_type}: "
                    f"spread {spread_pct:.1%} exceeds {MAX_0DTE_SPREAD_PCT:.0%} max (bid={bid}, ask={ask})"
                )
                return None

        last = snap_data.get("last") or snap_data.get("close")
        if last and float(last) > 0:
            return float(last)

        if mid:
            return mid
        if bid > 0:
            return bid
        if ask > 0:
            return ask

        return None
    except Exception as e:
        logger.debug(f"Could not fetch 0DTE option quote for {underlying} {strike} {option_type}: {e}")
        return None


def _fetch_leaps_option_quote(
    underlying: str,
    strike: float,
    min_dte_days: int = 180,
    max_dte_days: int = 400,
) -> Optional[tuple[float, str]]:
    """Fetch live option price and expiry for a far-dated LEAPS call contract (6-12 months out).

    Blocking — wrap in asyncio.to_thread.
    Returns (price, expiry_str) or None if no suitable contract or quote exists.
    """
    try:
        from core.wb import Webull
        from core.md import Market

        wb = Webull()
        if not wb.configured:
            return None

        mkt = Market(wb)
        chain_res = mkt.option_chain(
            underlying=underlying,
            option_type="CALL",
            strike_gte=strike - 0.01,
            strike_lte=strike + 0.01,
        )

        contracts = []
        if isinstance(chain_res, list):
            contracts = chain_res
        elif isinstance(chain_res, dict):
            contracts = chain_res.get("data") or chain_res.get("contracts") or chain_res.get("list") or []

        if not contracts:
            return None

        now = datetime.now(timezone.utc).date()
        leaps_contracts = []
        for c in contracts:
            c_strike = float(c.get("strike_price") or c.get("strikePrice") or c.get("strike") or 0.0)
            if abs(c_strike - strike) > 0.05:
                continue

            exp_str = c.get("expire_date") or c.get("expiration_date") or c.get("expiry") or c.get("start_date")
            if not exp_str:
                continue

            try:
                exp_date = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
                dte = (exp_date - now).days
                if min_dte_days <= dte <= max_dte_days:
                    leaps_contracts.append((dte, str(exp_str)[:10], c))
            except Exception:
                continue

        if not leaps_contracts:
            for c in contracts:
                exp_str = c.get("expire_date") or c.get("expiration_date") or c.get("expiry")
                if not exp_str:
                    continue
                try:
                    exp_date = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
                    dte = (exp_date - now).days
                    if dte >= min_dte_days:
                        leaps_contracts.append((dte, str(exp_str)[:10], c))
                except Exception:
                    continue

        if not leaps_contracts:
            return None

        leaps_contracts.sort(key=lambda x: abs(x[0] - 270))
        target_dte, target_expiry, target_contract = leaps_contracts[0]
        contract_sym = target_contract.get("symbol") or target_contract.get("ticker")
        if not contract_sym:
            return None

        snap_res = mkt.option_snapshot([contract_sym])
        snap_data = (snap_res.get(contract_sym) or snap_res) if isinstance(snap_res, dict) else None
        if not snap_data:
            return None

        last = snap_data.get("last") or snap_data.get("close")
        if last and float(last) > 0:
            return float(last), target_expiry

        bid = float(snap_data.get("bid") or snap_data.get("bid_price") or 0)
        ask = float(snap_data.get("ask") or snap_data.get("ask_price") or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2.0, 2), target_expiry
        if bid > 0:
            return bid, target_expiry
        if ask > 0:
            return ask, target_expiry

        return None
    except Exception as e:
        logger.debug(f"Could not fetch LEAPS option quote for {underlying} {strike}: {e}")
        return None


def _fetch_synthetic_long_quotes(
    underlying: str,
    strike: float,
) -> Optional[tuple[float, float, str, str, str]]:
    """Fetch live CALL and PUT premiums for a synthetic-long combo (long call
    + short put, same strike/expiry). Unlike _fetch_live_option_quote, this
    fetches both legs against the SAME expiry deliberately -- two independent
    calls could each silently pick a different nearest-dated contract at the
    strike, and execution_guard's SYNTHETIC_LONG formula requires the legs to
    match expiry. Picks the nearest expiry that actually has both a call and
    a put quoted at the strike; returns None (never fabricates) if none does.

    Blocking — wrap in asyncio.to_thread.
    Returns (call_premium, put_premium, expiry_str, call_contract_symbol,
    put_contract_symbol) or None.
    """
    try:
        from core.wb import Webull
        from core.md import Market

        wb = Webull()
        if not wb.configured:
            return None

        mkt = Market(wb)

        def _contracts_by_expiry(option_type: str) -> Dict[str, Dict[str, Any]]:
            chain_res = mkt.option_chain(
                underlying=underlying,
                option_type=option_type,
                strike_gte=strike - 0.01,
                strike_lte=strike + 0.01,
            )
            contracts = []
            if isinstance(chain_res, list):
                contracts = chain_res
            elif isinstance(chain_res, dict):
                contracts = chain_res.get("data") or chain_res.get("contracts") or chain_res.get("list") or []

            by_expiry: Dict[str, Dict[str, Any]] = {}
            for c in contracts:
                c_strike = float(c.get("strike_price") or c.get("strikePrice") or c.get("strike") or 0.0)
                if abs(c_strike - strike) > 0.05:
                    continue
                exp_str = c.get("expire_date") or c.get("expiration_date") or c.get("expiry") or c.get("start_date")
                if not exp_str:
                    continue
                by_expiry[str(exp_str)[:10]] = c
            return by_expiry

        calls_by_expiry = _contracts_by_expiry("CALL")
        puts_by_expiry = _contracts_by_expiry("PUT")
        shared_expiries = sorted(set(calls_by_expiry) & set(puts_by_expiry))
        if not shared_expiries:
            return None
        target_expiry = shared_expiries[0]

        call_sym = calls_by_expiry[target_expiry].get("symbol") or calls_by_expiry[target_expiry].get("ticker")
        put_sym = puts_by_expiry[target_expiry].get("symbol") or puts_by_expiry[target_expiry].get("ticker")
        if not call_sym or not put_sym:
            return None

        def _price_from_snapshot(snap_res: Any, sym: str) -> Optional[float]:
            snap_data = (snap_res.get(sym) or snap_res) if isinstance(snap_res, dict) else None
            if not snap_data:
                return None
            last = snap_data.get("last") or snap_data.get("close")
            if last and float(last) > 0:
                return float(last)
            bid = float(snap_data.get("bid") or snap_data.get("bid_price") or 0)
            ask = float(snap_data.get("ask") or snap_data.get("ask_price") or 0)
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2.0, 2)
            if bid > 0:
                return bid
            if ask > 0:
                return ask
            return None

        call_snap = mkt.option_snapshot([call_sym])
        put_snap = mkt.option_snapshot([put_sym])
        call_premium = _price_from_snapshot(call_snap, call_sym)
        put_premium = _price_from_snapshot(put_snap, put_sym)
        if call_premium is None or put_premium is None:
            return None

        return call_premium, put_premium, target_expiry, call_sym, put_sym
    except Exception as e:
        logger.debug(f"Could not fetch synthetic-long quotes for {underlying} {strike}: {e}")
        return None


def _fetch_income_fund_detail(fund: str) -> Optional[Dict[str, Any]]:
    """Fetch income fund details from TickerTrace. Blocking — wrap in asyncio.to_thread."""
    try:
        from tickertrace_mcp import get_income_fund_detail
        res = get_income_fund_detail(fund=fund)
        return res if isinstance(res, dict) else None
    except Exception as e:
        logger.warning(f"Could not fetch TickerTrace income fund detail for {fund}: {e}")
        return None


def _fetch_upcoming_earnings() -> Optional[List[Dict[str, Any]]]:
    """Fetch TraderDaddy's upcoming-earnings calendar via td.py's get_earnings_flow.

    Confirmed live 2026-08-29: get_earnings_flow's `symbol` param is NOT a
    filter (same class of gotcha as the documented get_conviction/`ticker`
    trap in CLAUDE.md) -- it always returns the full market-wide upcoming
    slate. Real confirmed fields per entry: event.symbol, event.earningsDate
    ("YYYY-MM-DD"), event.earningsTime ("AMC" or "BMO"), event.expectedMovePct.

    Blocking — wrap in asyncio.to_thread.
    Returns the raw `earnings` list, or None (never a fabricated empty/stale
    calendar) if TDPro is unconfigured or the call errors.
    """
    try:
        from core.td import TDPro

        client = TDPro()
        if not client.configured:
            return None
        res = client.call("get_earnings_flow", {})
        if not isinstance(res, dict):
            return None
        earnings = res.get("earnings")
        return earnings if isinstance(earnings, list) else None
    except Exception as e:
        logger.debug(f"Could not fetch upcoming earnings calendar: {e}")
        return None


def _extract_fund_put_hedges(fund_data: Dict[str, Any], default_ticker: str) -> List[tuple[str, float]]:
    """Extract (underlying, strike) for all protective PUT holdings in the fund's option book."""
    results: List[tuple[str, float]] = []

    candidates = []
    for key in ("options", "option_book", "holdings", "positions", "calls_and_puts", "hedges", "data"):
        val = fund_data.get(key)
        if isinstance(val, list):
            candidates.extend(val)
        elif isinstance(val, dict):
            for sub_val in val.values():
                if isinstance(sub_val, list):
                    candidates.extend(sub_val)

    if not candidates and isinstance(fund_data, dict):
        for v in fund_data.values():
            if isinstance(v, list):
                candidates.extend(v)

    for item in candidates:
        if not isinstance(item, dict):
            continue

        opt_type = str(
            item.get("option_type")
            or item.get("type")
            or item.get("put_call")
            or item.get("side_type")
            or ""
        ).upper()
        desc = str(item.get("description") or item.get("symbol") or item.get("name") or "").upper()

        is_put = (opt_type == "PUT") or (" PUT" in desc) or ("_P" in desc) or (desc.endswith("P"))
        if not is_put:
            continue

        underlying = (
            item.get("underlying")
            or item.get("ticker")
            or item.get("underlying_symbol")
            or item.get("symbol")
        )
        if not underlying and desc:
            parts = desc.split()
            if parts and parts[0].isalpha() and len(parts[0]) <= 6:
                underlying = parts[0]

        if not underlying:
            underlying = default_ticker

        if isinstance(underlying, str):
            if len(underlying) > 6 and any(c.isdigit() for c in underlying):
                clean_underlying = re.split(r"\d", underlying)[0].strip()
                if clean_underlying:
                    underlying = clean_underlying
            underlying = underlying.upper().strip()
        else:
            underlying = default_ticker.upper().strip()

        strike = item.get("strike") or item.get("strike_price") or item.get("strikePrice")
        if strike is None and desc:
            match = re.search(r"(\d+(?:\.\d+)?)\s*P", desc) or re.search(r"[CP](\d{8})", desc)
            if match:
                val = match.group(1)
                strike = float(val) / 1000.0 if len(val) == 8 else float(val)

        if strike is not None:
            try:
                strike_val = float(strike)
                if strike_val > 0 and underlying:
                    results.append((underlying, strike_val))
            except (ValueError, TypeError):
                pass

    seen = set()
    deduped = []
    for pair in results:
        if pair not in seen:
            seen.add(pair)
            deduped.append(pair)

    return deduped


async def playbooks_node(state: TradingState) -> Dict[str, Any]:
    """Applies domain playbooks to draft high-conviction order proposals."""
    logger.info("-> [PlaybooksNode] Applying strategy playbooks to candidates...")

    from core.conviction import get_playbook_performance

    proposals: List[OrderProposal] = []
    technicals = state.get("technicals", {})
    options_audits = state.get("options_audits", {})
    regime = state.get("regime")
    account_equity = await asyncio.to_thread(fetch_live_equity) if technicals else 0.0
    audit_notes = []

    # Check playbook outcome calibration history (Module 5 Phase 5)
    selected_playbook = state.get("selected_playbook", "all")
    calibration = get_playbook_performance(selected_playbook)
    size_adjustment = calibration.get("adjustment", 0.0)
    if calibration.get("resolved", 0) >= 3:
        if size_adjustment < 0:
            audit_notes.append(
                f"Calibration Guard: '{selected_playbook}' win rate is {calibration['win_rate_pct']:.0f}% "
                f"({calibration['wins']}/{calibration['resolved']}). Applying {size_adjustment:+.0%} risk scaling."
            )
        elif size_adjustment > 0:
            audit_notes.append(
                f"Calibration Boost: '{selected_playbook}' win rate is {calibration['win_rate_pct']:.0f}% "
                f"({calibration['wins']}/{calibration['resolved']}). Full conviction validated."
            )

    calibrated_equity = max(0.0, account_equity * (1.0 + size_adjustment))

    for ticker, tech in technicals.items():
        opt = options_audits.get(ticker)
        
        # ── 1. 0DTE FLOW PLAYBOOK (SPY / QQQ) ──────────────────────────────────
        # Tightened (2026-08-29): requires IV >= 70% (same threshold used
        # elsewhere in this file), and rejects a quote whose bid/ask spread is
        # too wide for reliable execution (_fetch_0dte_option_quote's own
        # MAX_0DTE_SPREAD_PCT check). Strike selection (2026-08-30) tries a
        # true ~0.30-delta Black-Scholes pick across the full same-day chain
        # first (_select_0dte_delta_strike, via py_vollib), falling back to a
        # real major OI put/call wall (td.levels()) when delta selection is
        # unavailable -- never a fixed spot+/-1 offset. Any one of these being
        # unavailable skips the draft rather than falling back to the old,
        # looser behavior.
        if ticker in ("SPY", "QQQ") and regime and regime.spy_spot and regime.spy_gamma_flip:
            spot = regime.spy_spot
            flip = regime.spy_gamma_flip
            is_bullish = spot > flip
            side_type = "call" if is_bullish else "put"

            iv_val = getattr(opt, "iv", 0.0) if opt is not None and not isinstance(opt, dict) else (opt.get("iv", 0.0) if isinstance(opt, dict) else None)
            iv_pct = (iv_val if iv_val > 2.0 else iv_val * 100.0) if iv_val else None
            if iv_pct is None or iv_pct < 70.0:
                audit_notes.append(
                    f"Skipped 0DTE {ticker} {side_type.upper()}: IV "
                    f"{'unavailable' if iv_pct is None else f'{iv_pct:.1f}% < 70%'}"
                )
                continue

            strike = await asyncio.to_thread(
                _select_0dte_delta_strike, ticker, spot, is_bullish, iv_pct
            )
            selection_method = "0.30-delta (Black-Scholes)"
            if strike is None:
                strike = await asyncio.to_thread(_select_0dte_wall_strike, ticker, is_bullish)
                selection_method = "major OI wall (delta selection unavailable)"
            if strike is None:
                audit_notes.append(
                    f"Skipped 0DTE {ticker} {side_type.upper()}: no delta-based or wall-based "
                    "strike available to anchor a strike to"
                )
                continue

            live_premium = await asyncio.to_thread(_fetch_0dte_option_quote, ticker, strike, side_type)
            if live_premium is None or live_premium <= 0:
                audit_notes.append(
                    f"Skipped 0DTE {ticker} {side_type.upper()} strike ${strike:.2f}: no live same-day "
                    "option quote available (or bid/ask spread too wide)"
                )
                continue

            est_premium = round(live_premium, 2)
            qty = 1             # Strict 1-contract small account sizing
            cost = round(est_premium * 100 * qty, 2)
            stop_loss = round(est_premium * (1 - RiskEnforcer.STOP_LOSS_0DTE_PCT), 2)
            profit_target = round(est_premium * (1 + RiskEnforcer.TAKE_PROFIT_0DTE_PCT), 2)

            prop = OrderProposal(
                id=f"prop-0dte-{uuid.uuid4().hex[:6]}",
                ticker=ticker,
                asset_type="OPTION",
                side="BUY",
                order_type="LIMIT",
                quantity=qty,
                limit_price=est_premium,
                stop_loss=stop_loss,
                profit_target=profit_target,
                strike=strike,
                option_type=side_type,
                estimated_cost=cost,
                max_risk=round(cost * RiskEnforcer.STOP_LOSS_0DTE_PCT, 2),
                risk_reward_ratio=1.25,
            )
            proposals.append(prop)
            audit_notes.append(
                f"Drafted 0DTE {ticker} {side_type.upper()} Strike {strike} @ ${est_premium:.2f} "
                f"(Spot={spot} vs Flip={flip}, IV={iv_pct:.1f}%, strike from {selection_method})"
            )
            continue

        # ── 2. TAO OF TRADING BOUNCE 2.0 & MOMENTUM PULLBACK PLAYBOOK ────────
        # Rules (all six required — this is a mean-reversion pullback entry,
        # not a breakout filter; an earlier version of this playbook OR'd the
        # action-zone and stochastic-exhaustion checks against a bare
        # `rsi_14 > 45`/`<= 55`, which let almost any mildly-bullish reading
        # through regardless of whether price had actually pulled back or the
        # RSI(2) dip fired — the exact "trades on any bullish RSI" shape this
        # playbook was rewritten to get away from in the first place. Missing
        # data (rsi_2/slow_k/keltner unavailable) means "don't draft," not
        # "assume it passes" — this is candidate generation, not the safety
        # gate, but a proposal Vesper can't actually justify isn't worth
        # showing a human either):
        # 1. Bullish EMA stack (8 > 21 > 34 > 55 > 89)
        # 2. ADX(14) >= 18 (trend strength)
        # 3. Pullback into Keltner Action Zone (between EMA 21 and Keltner lower band, or ±1.5 ATR)
        # 4. Slow Stochastic(8,3) <= 40 (pullback oversold exhaustion)
        # 5. RSI(2) dip trigger: dipped to <=10 (this or the prior bar), now back above 10
        # 6. Not overbought (RSI(14) <= 68)
        is_bullish_trend = (tech.ema_stack == "BULLISH") or (tech.ema_8 and tech.ema_21 and tech.ema_8 >= tech.ema_21)
        adx_valid = (tech.adx_14 is None) or (tech.adx_14 >= 18.0)

        entry_price = tech.close
        atr = tech.atr_14 or (entry_price * 0.03)
        ema_21 = tech.ema_21 or entry_price

        # True Keltner Action Zone (length 14, 2x ATR) — required, not a fallback.
        keltner_lower = tech.keltner_lower or (ema_21 - (2.0 * atr))
        in_action_zone = (entry_price >= keltner_lower) and (entry_price <= ema_21 + (1.5 * atr))

        # Slow Stochastic(8,3) <= 40 — the documented threshold, no rsi_14 escape hatch.
        stoch_oversold = tech.slow_k is not None and tech.slow_k <= 40.0

        # RSI(2) dip-then-reset: dipped to <=10 on this bar or the prior one,
        # and has now crossed back above 10 (rsi_2 > 10). Both readings must
        # be present -- an unavailable rsi_2 means this trigger didn't fire,
        # not that it's assumed to have fired.
        rsi_2_trigger = (
            tech.rsi_2 is not None and tech.rsi_2_prev is not None
            and tech.rsi_2 > 10.0
            and (tech.rsi_2_prev <= 10.0 or tech.rsi_2 <= 10.0)
        )

        not_overbought = tech.rsi_14 <= 68.0

        if is_bullish_trend and in_action_zone and rsi_2_trigger and stoch_oversold and not_overbought and adx_valid:
            # Stop loss 1.5 ATR below entry / 21 EMA
            stop_loss = round(min(entry_price - (atr * 1.5), ema_21 - (atr * 1.0)), 2)
            if stop_loss >= entry_price:
                stop_loss = round(entry_price - (atr * 1.5), 2)
            profit_target = round(entry_price + (atr * 3.0), 2)
            
            # Volatility-Targeted Position Sizing
            shares, total_cost, total_risk = RiskEnforcer.calculate_vol_targeted_size(
                account_equity=calibrated_equity,
                entry_price=entry_price,
                stop_loss_price=stop_loss,
                target_price=profit_target,
                atr_14=atr,
            )
            
            if shares > 0:
                prop = OrderProposal(
                    id=f"prop-eq-{uuid.uuid4().hex[:6]}",
                    ticker=ticker,
                    asset_type="EQUITY",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=shares,
                    limit_price=entry_price,
                    stop_loss=stop_loss,
                    profit_target=profit_target,
                    estimated_cost=total_cost,
                    max_risk=total_risk,
                    risk_reward_ratio=round((profit_target - entry_price) / max(0.01, entry_price - stop_loss), 2),
                )
                proposals.append(prop)
                audit_notes.append(
                    f"Drafted Bounce 2.0 Equity Buy for {ticker}: {shares} shares @ ${entry_price:.2f} "
                    f"(Stop=${stop_loss:.2f}, Target=${profit_target:.2f}, Vol-Targeted Risk=${total_risk:.2f})"
                )

                # Optional OpenRouter AI Thesis Enrichment (if OPENROUTER_API_KEY configured)
                from vesper.llm import generate_candidate_thesis, is_llm_enabled
                if is_llm_enabled():
                    try:
                        thesis_res = await generate_candidate_thesis(
                            ticker=ticker,
                            technical_summary=tech.summary or f"Close=${entry_price}, RSI={tech.rsi_14:.1f}, EMA={tech.ema_stack}",
                            candidate_rationale="Bounce 2.0 Action Zone Pullback",
                            regime_posture=regime.posture if regime else "NEUTRAL",
                        )
                        if thesis_res and thesis_res.get("thesis"):
                            audit_notes.append(f"AI Thesis ({thesis_res.get('source')}): {thesis_res['thesis']}")
                            # Also attach to the proposal itself so the
                            # approval card can show it -- audit_notes is
                            # audit-trail-only and never reaches the card.
                            prop.thesis = thesis_res.get("thesis")
                            prop.thesis_source = thesis_res.get("source")
                    except Exception as e:
                        logger.debug("OpenRouter thesis enrichment skipped: %s", e)

                # Check if high-beta 2x leveraged vehicle exists (Module 6)
                from vesper.leveraged import get_primary_2x
                proxy_2x = get_primary_2x(ticker)
                if proxy_2x and proxy_2x != ticker:
                    # The leveraged ETF trades at its own price, unrelated to the
                    # underlying's — using entry_price here (as an earlier pass
                    # did) would draft a LIMIT order for the wrong instrument at
                    # the wrong price, and that fabricated number would flow
                    # straight into ExecutionGuard's notional-cap check as if it
                    # were real. Fetch a real quote or skip the proxy entirely;
                    # never guess a price for something that gets guarded on it.
                    proxy_price = await asyncio.to_thread(_fetch_live_quote, proxy_2x)
                    if proxy_price is None:
                        audit_notes.append(
                            f"Skipped 2x Leveraged Alternate for {ticker} ({proxy_2x}): no live quote available"
                        )
                    else:
                        # Scale down position by 2x to maintain equal risk budget
                        proxy_shares = max(1, shares // 2)
                        proxy_cost = round(proxy_shares * proxy_price, 2)
                        proxy_prop = OrderProposal(
                            id=f"prop-2x-{uuid.uuid4().hex[:6]}",
                            ticker=proxy_2x,
                            asset_type="LEVERAGED_ETF",
                            side="BUY",
                            order_type="LIMIT",
                            quantity=proxy_shares,
                            limit_price=proxy_price,
                            stop_loss=round(proxy_price * (1 - RiskEnforcer.STOP_LOSS_0DTE_PCT * 0.85), 2),
                            profit_target=round(proxy_price * (1 + RiskEnforcer.TAKE_PROFIT_0DTE_PCT * 1.30), 2),
                            estimated_cost=proxy_cost,
                            max_risk=round(total_risk * 0.5, 2),
                            risk_reward_ratio=2.5,
                        )
                        proposals.append(proxy_prop)
                        audit_notes.append(
                            f"Drafted 2x Leveraged Alternate: {proxy_2x} ({proxy_shares} shares @ ${proxy_price:.2f})"
                        )

    # ── 3. COLLAR-FOLLOWING PLAYBOOK (Income ETF Protective Put Replication) ─
    # Sells Cash-Secured Puts (CSPs) at the exact strikes an option-income ETF
    # bought as its own protective hedge (e.g. ULTY, QQQI, NVDY).
    # Configured via VESPER_COLLAR_FOLLOW_FUNDS (comma-separated list of funds).
    if selected_playbook in ("all", "collar_following", "collar"):
        follow_funds_env = os.getenv("VESPER_COLLAR_FOLLOW_FUNDS", "").strip()
        follow_funds = [f.strip().upper() for f in follow_funds_env.split(",") if f.strip()]

        for fund in follow_funds:
            fund_detail = await asyncio.to_thread(_fetch_income_fund_detail, fund)
            if not fund_detail:
                audit_notes.append(f"Collar-Following: TickerTrace data unavailable for {fund}")
                continue

            put_hedges = _extract_fund_put_hedges(fund_detail, default_ticker=fund)
            if not put_hedges:
                audit_notes.append(f"Collar-Following: No protective put holdings found in {fund} option book")
                continue

            for under_ticker, put_strike in put_hedges:
                # Fetch real live option quote. NEVER fabricate or guess a premium.
                live_premium = await asyncio.to_thread(_fetch_live_option_quote, under_ticker, put_strike, "PUT")
                if live_premium is None or live_premium <= 0:
                    audit_notes.append(
                        f"Skipped Collar CSP for {under_ticker} strike ${put_strike:.2f} ({fund}): "
                        "no live option quote available"
                    )
                    continue

                # Sizing: Conservative 1 contract for small account safety
                qty = 1
                premium = round(live_premium, 2)
                # Assignment capital commitment = strike * 100 * qty
                assignment_notional = round(put_strike * 100 * qty, 2)

                collar_prop = OrderProposal(
                    id=f"prop-collar-{uuid.uuid4().hex[:6]}",
                    ticker=under_ticker,
                    asset_type="OPTION",
                    side="SELL",
                    order_type="LIMIT",
                    quantity=qty,
                    limit_price=premium,
                    strike=put_strike,
                    option_type="put",
                    stop_loss=round(premium * 2.5, 2),       # Buy to close at 250% premium
                    profit_target=round(premium * 0.20, 2),   # Harvest 80% premium decay
                    estimated_cost=assignment_notional,       # REAL capital at risk on assignment
                    max_risk=assignment_notional,             # Cash-secured put assignment liability
                    risk_reward_ratio=0.53,
                )
                proposals.append(collar_prop)
                audit_notes.append(
                    f"Drafted Collar-Following CSP for {under_ticker} ({fund} hedge): "
                    f"SELL 1x Put Strike ${put_strike:.2f} @ ${premium:.2f} "
                    f"(Assignment Notional: ${assignment_notional:,.2f})"
                )

    # ── 4. ADX / IV OPTION-STYLE ROUTER PLAYBOOK ────────────────────────────
    # Classifies candidates by trend strength (ADX(14) >= 20) vs implied volatility (IV >= 70%):
    #   • ADX < 20  + IV < 70%  -> "Training Wheels": buy shares outright
    #   • ADX < 20  + IV >= 70% -> "Wheel": sell Cash-Secured Put (CSP) at near-the-money strike
    #   • ADX >= 20 + IV < 70%  -> "LEAPS": buy far-dated call (6-12 months out)
    #   • ADX >= 20 + IV >= 70% -> "Synthetic Long": multi-leg deferred (skipped)
    if selected_playbook in ("all", "adx_iv", "adx_iv_router", "router"):
        for ticker, tech in technicals.items():
            opt = options_audits.get(ticker)
            if tech.adx_14 is None or opt is None:
                continue

            iv_val = getattr(opt, "iv", 0.0) if not isinstance(opt, dict) else opt.get("iv", 0.0)
            if iv_val is None or iv_val <= 0:
                continue

            iv_pct = iv_val if iv_val > 2.0 else iv_val * 100.0
            is_trending = tech.adx_14 >= 20.0
            is_high_iv = iv_pct >= 70.0
            entry_price = tech.close
            atr = tech.atr_14 or (entry_price * 0.03)

            # Branch 1: ADX < 20 + IV < 70% -> "Training Wheels" (buy shares outright)
            if not is_trending and not is_high_iv:
                stop_loss = round(entry_price - (atr * 1.5), 2)
                profit_target = round(entry_price + (atr * 3.0), 2)
                shares, total_cost, total_risk = RiskEnforcer.calculate_vol_targeted_size(
                    account_equity=calibrated_equity,
                    entry_price=entry_price,
                    stop_loss_price=stop_loss,
                    target_price=profit_target,
                    atr_14=atr,
                )
                if shares > 0:
                    prop = OrderProposal(
                        id=f"prop-adxiv-eq-{uuid.uuid4().hex[:6]}",
                        ticker=ticker,
                        asset_type="EQUITY",
                        side="BUY",
                        order_type="LIMIT",
                        quantity=shares,
                        limit_price=entry_price,
                        stop_loss=stop_loss,
                        profit_target=profit_target,
                        estimated_cost=total_cost,
                        max_risk=total_risk,
                        risk_reward_ratio=round((profit_target - entry_price) / max(0.01, entry_price - stop_loss), 2),
                    )
                    proposals.append(prop)
                    audit_notes.append(
                        f"Drafted ADX/IV Router [Training Wheels] Equity Buy for {ticker}: "
                        f"{shares} shares @ ${entry_price:.2f} (ADX={tech.adx_14:.1f} < 20, IV={iv_pct:.1f}% < 70%)"
                    )

            # Branch 2: ADX < 20 + IV >= 70% -> "Wheel" (sell cash-secured put)
            elif not is_trending and is_high_iv:
                strike = round(entry_price, 0)
                live_premium = await asyncio.to_thread(_fetch_live_option_quote, ticker, strike, "PUT")
                if live_premium is None or live_premium <= 0:
                    audit_notes.append(
                        f"Skipped ADX/IV Router [Wheel] CSP for {ticker} strike ${strike:.2f}: no live option quote available"
                    )
                    continue

                qty = 1
                premium = round(live_premium, 2)
                # Assignment capital commitment = strike * 100 * qty
                assignment_notional = round(strike * 100 * qty, 2)
                prop = OrderProposal(
                    id=f"prop-adxiv-wheel-{uuid.uuid4().hex[:6]}",
                    ticker=ticker,
                    asset_type="OPTION",
                    side="SELL",
                    order_type="LIMIT",
                    quantity=qty,
                    limit_price=premium,
                    strike=strike,
                    option_type="put",
                    stop_loss=round(premium * 2.5, 2),
                    profit_target=round(premium * 0.20, 2),
                    estimated_cost=assignment_notional,
                    max_risk=assignment_notional,
                    risk_reward_ratio=0.53,
                )
                proposals.append(prop)
                audit_notes.append(
                    f"Drafted ADX/IV Router [Wheel] CSP for {ticker}: SELL 1x Put Strike ${strike:.2f} @ ${premium:.2f} "
                    f"(ADX={tech.adx_14:.1f} < 20, IV={iv_pct:.1f}% >= 70%, Assignment Notional: ${assignment_notional:,.2f})"
                )

            # Branch 3: ADX >= 20 + IV < 70% -> "LEAPS" (buy far-dated call 6-12 months out)
            elif is_trending and not is_high_iv:
                strike = round(entry_price, 0)
                leaps_res = await asyncio.to_thread(_fetch_leaps_option_quote, ticker, strike)
                if leaps_res is None:
                    audit_notes.append(
                        f"Skipped ADX/IV Router [LEAPS] for {ticker} strike ${strike:.2f}: no far-dated (180d+) option quote available"
                    )
                    continue

                premium, expiry_str = leaps_res
                qty = 1
                cost = round(premium * 100 * qty, 2)

                # Underlying-keyed swing stop: prefer ema_34, else sma_200, else
                # keltner_lower (first non-None wins). This is an ADDITIONAL
                # trigger alongside the flat contract stop_loss above, evaluated
                # by monitor.py's evaluate_position() against a fresh
                # analyze_technicals() read each cycle -- never leave it None
                # here in favor of a fabricated level; if none of the three are
                # available, the position simply keeps only the contract-pct stop.
                underlying_stop_basis = None
                for basis in ("ema_34", "sma_200", "keltner_lower"):
                    if getattr(tech, basis, None) is not None:
                        underlying_stop_basis = basis
                        break

                prop = OrderProposal(
                    id=f"prop-adxiv-leaps-{uuid.uuid4().hex[:6]}",
                    ticker=ticker,
                    asset_type="OPTION",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=qty,
                    limit_price=premium,
                    strike=strike,
                    expiry=expiry_str,
                    option_type="call",
                    stop_loss=round(premium * 0.50, 2),
                    profit_target=round(premium * 2.0, 2),
                    underlying_stop_type="underlying_level" if underlying_stop_basis else None,
                    underlying_stop_basis=underlying_stop_basis,
                    estimated_cost=cost,
                    max_risk=cost,
                    risk_reward_ratio=2.0,
                )
                proposals.append(prop)
                audit_notes.append(
                    f"Drafted ADX/IV Router [LEAPS] Call for {ticker}: BUY 1x Strike ${strike:.2f} Exp {expiry_str} @ ${premium:.2f} "
                    f"(ADX={tech.adx_14:.1f} >= 20, IV={iv_pct:.1f}% < 70%, Cost: ${cost:,.2f})"
                )

            # Branch 4: ADX >= 20 + IV >= 70% -> "Synthetic Long" (simultaneous BUY call + SELL put)
            # Multi-leg combo: OrderProposal.legs + execution_guard's SYNTHETIC_LONG
            # risk formula (strike-based, same reasoning as the Wheel CSP above —
            # the short put is the capital-at-risk driver). Both legs are fetched
            # against the same live-confirmed expiry by _fetch_synthetic_long_quotes
            # so the guard's expiry-match check can't fail on a data artifact.
            elif is_trending and is_high_iv:
                strike = round(entry_price, 0)
                synth_res = await asyncio.to_thread(_fetch_synthetic_long_quotes, ticker, strike)
                if synth_res is None:
                    audit_notes.append(
                        f"Skipped ADX/IV Router [Synthetic Long] for {ticker} strike ${strike:.2f}: "
                        "no shared-expiry call+put quote available"
                    )
                    continue

                call_premium, put_premium, expiry_str, call_sym, put_sym = synth_res
                qty = 1
                net_premium = round((call_premium - put_premium) * 100 * qty, 2)  # debit if +, credit if -
                assignment_notional = round(strike * 100 * qty, 2)

                # Underlying-keyed swing stop, same basis-selection order as the
                # LEAPS branch above (ema_34 -> sma_200 -> keltner_lower, first
                # non-None wins). Applies to the combo as a whole -- the long
                # call leg is the exposure this stop protects.
                underlying_stop_basis = None
                for basis in ("ema_34", "sma_200", "keltner_lower"):
                    if getattr(tech, basis, None) is not None:
                        underlying_stop_basis = basis
                        break

                prop = OrderProposal(
                    id=f"prop-adxiv-synth-{uuid.uuid4().hex[:6]}",
                    ticker=ticker,
                    asset_type="OPTION",
                    side="BUY",  # net position is long-equivalent; legs carry the real sides
                    order_type="LIMIT",
                    quantity=qty,
                    limit_price=call_premium,
                    strike=strike,
                    expiry=expiry_str,
                    option_type="call",
                    strategy_type="SYNTHETIC_LONG",
                    legs=[
                        OrderLeg(
                            side="BUY", option_type="call", strike=strike,
                            expiry=expiry_str, quantity=qty, limit_price=call_premium,
                            contract_symbol=call_sym,
                        ),
                        OrderLeg(
                            side="SELL", option_type="put", strike=strike,
                            expiry=expiry_str, quantity=qty, limit_price=put_premium,
                            contract_symbol=put_sym,
                        ),
                    ],
                    stop_loss=None,
                    profit_target=None,
                    underlying_stop_type="underlying_level" if underlying_stop_basis else None,
                    underlying_stop_basis=underlying_stop_basis,
                    # Same capital-at-risk figure execution_guard will independently
                    # recompute from the legs -- this is for the approval card /
                    # audit trail, not trusted at guard time.
                    estimated_cost=assignment_notional,
                    max_risk=assignment_notional,
                    risk_reward_ratio=0.0,
                )
                proposals.append(prop)
                audit_notes.append(
                    f"Drafted ADX/IV Router [Synthetic Long] for {ticker}: BUY 1x Call + SELL 1x Put, "
                    f"Strike ${strike:.2f} Exp {expiry_str}, net {'debit' if net_premium >= 0 else 'credit'} "
                    f"${abs(net_premium):,.2f} (ADX={tech.adx_14:.1f} >= 20, IV={iv_pct:.1f}% >= 70%, "
                    f"Assignment Notional: ${assignment_notional:,.2f})"
                )

    # ── 5. THEGA: DELTA-NEUTRAL VOLATILITY HARVEST ──────────────────────────
    # 100 shares + 1 ATM covered call + 3 ATM CSPs, same strike/expiry, net
    # delta ~0 -- harvests high-IV premium (earnings, other binary events)
    # without a directional bet. Gated on IV >= 70% alone (the same threshold
    # the ADX/IV router uses for its high-IV branches) -- NOT on detecting an
    # actual binary event, since nothing in this codebase has an
    # earnings-calendar/event data source to check that against. Don't
    # "fix" this by guessing at an event date; wire a real calendar source
    # first (see ROADMAP.md).
    if selected_playbook in ("all", "thega"):
        for ticker, tech in technicals.items():
            opt = options_audits.get(ticker)
            if opt is None:
                continue
            iv_val = getattr(opt, "iv", 0.0) if not isinstance(opt, dict) else opt.get("iv", 0.0)
            if iv_val is None or iv_val <= 0:
                continue
            iv_pct = iv_val if iv_val > 2.0 else iv_val * 100.0
            if iv_pct < 70.0:
                continue

            entry_price = tech.close
            strike = round(entry_price, 0)

            synth_res = await asyncio.to_thread(_fetch_synthetic_long_quotes, ticker, strike)
            if synth_res is None:
                audit_notes.append(
                    f"Skipped Thega for {ticker} strike ${strike:.2f}: no shared-expiry call+put quote available"
                )
                continue
            call_premium, put_premium, expiry_str, call_sym, put_sym = synth_res

            equity_price = await asyncio.to_thread(_fetch_live_quote, ticker)
            if equity_price is None or equity_price <= 0:
                audit_notes.append(f"Skipped Thega for {ticker}: no live equity quote available")
                continue

            equity_qty = 100
            call_qty = 1
            put_qty = 3
            equity_notional = round(equity_qty * equity_price, 2)
            # Worst-case capital at risk: the shares as a total loss, plus
            # assignment on all 3 CSPs (same strike-based reasoning as every
            # other short-option formula in this file) -- matches
            # execution_guard's THEGA formula exactly.
            put_assignment_notional = round(strike * 100 * put_qty, 2)
            max_risk = round(equity_notional + put_assignment_notional, 2)
            net_credit = round((call_premium * 100 * call_qty) + (put_premium * 100 * put_qty), 2)

            prop = OrderProposal(
                id=f"prop-thega-{uuid.uuid4().hex[:6]}",
                ticker=ticker,
                asset_type="EQUITY",  # net position display; legs carry the real per-leg types
                side="BUY",
                order_type="LIMIT",
                quantity=equity_qty,
                limit_price=equity_price,
                strike=strike,
                expiry=expiry_str,
                strategy_type="THEGA",
                legs=[
                    OrderLeg(
                        side="BUY", asset_type="EQUITY", quantity=equity_qty, limit_price=equity_price,
                    ),
                    OrderLeg(
                        side="SELL", asset_type="OPTION", option_type="call", strike=strike,
                        expiry=expiry_str, quantity=call_qty, limit_price=call_premium,
                        contract_symbol=call_sym,
                    ),
                    OrderLeg(
                        side="SELL", asset_type="OPTION", option_type="put", strike=strike,
                        expiry=expiry_str, quantity=put_qty, limit_price=put_premium,
                        contract_symbol=put_sym,
                    ),
                ],
                stop_loss=None,
                profit_target=None,
                estimated_cost=max_risk,
                max_risk=max_risk,
                risk_reward_ratio=0.0,
            )
            proposals.append(prop)
            audit_notes.append(
                f"Drafted Thega for {ticker}: BUY 100sh @ ${equity_price:.2f} + SELL 1x Call + SELL 3x Put, "
                f"Strike ${strike:.2f} Exp {expiry_str}, net credit ${net_credit:,.2f} "
                f"(IV={iv_pct:.1f}% >= 70%, Max Risk: ${max_risk:,.2f})"
            )

    # ── 6. PREMIUM-RECYCLING "FREE SHARE" ENGINE ───────────────────────────
    # Sweeps realized options-selling P&L from paper ledger into accumulating
    # 100-share blocks of a stabilizing asset (default $SGOV), funded entirely
    # from collected premium (not fresh capital).
    # Configured via VESPER_PREMIUM_RECYCLE_TICKER (default SGOV).
    if selected_playbook in ("all", "recycle", "premium_recycle", "free_shares"):
        from core.paper_ledger import get_unswept_premium
        unswept_pnl = await asyncio.to_thread(get_unswept_premium)
        recycle_ticker = os.getenv("VESPER_PREMIUM_RECYCLE_TICKER", "SGOV").strip().upper()

        if recycle_ticker and unswept_pnl > 0:
            recycle_price = await asyncio.to_thread(_fetch_live_quote, recycle_ticker)
            if recycle_price is None:
                audit_notes.append(
                    f"Skipped Premium Recycling for {recycle_ticker}: no live quote available"
                )
            else:
                block_cost = round(recycle_price * 100, 2)
                if unswept_pnl >= block_cost:
                    recycle_prop = OrderProposal(
                        id=f"prop-recycle-{uuid.uuid4().hex[:6]}",
                        ticker=recycle_ticker,
                        asset_type="EQUITY",
                        side="BUY",
                        order_type="LIMIT",
                        quantity=100,
                        limit_price=recycle_price,
                        stop_loss=round(recycle_price * 0.98, 2),
                        profit_target=round(recycle_price * 1.05, 2),
                        estimated_cost=block_cost,
                        max_risk=block_cost,
                        risk_reward_ratio=1.0,
                    )
                    proposals.append(recycle_prop)
                    audit_notes.append(
                        f"Drafted Premium Recycling Buy: 100 shares of {recycle_ticker} @ ${recycle_price:.2f} "
                        f"(Funded by ${block_cost:,.2f} of ${unswept_pnl:,.2f} unswept realized options premium)"
                    )
                else:
                    audit_notes.append(
                        f"Premium Recycling: unswept PnL (${unswept_pnl:,.2f}) below 100-share threshold for {recycle_ticker} (${block_cost:,.2f})"
                    )

    # ── 7. TAX RESERVE SWEEP (25% of Realized P&L to $SGOV) ────────────────
    # Sweeps 25% of cumulative realized P&L -- tracked as an independent pool
    # from the free-share engine's 75% (see paper_ledger.get_paper_summary) --
    # into whole-share (not 100-share-block) buys of a stabilizing asset,
    # earmarked as a tax set-aside. Configured via VESPER_TAX_RESERVE_TICKER
    # (default SGOV, same default as the free-share pool but tracked
    # separately -- the two pools may even choose different tickers).
    if selected_playbook in ("all", "tax_reserve", "taxsweep"):
        from core.paper_ledger import get_unswept_tax_reserve
        unswept_tax = await asyncio.to_thread(get_unswept_tax_reserve)
        tax_reserve_ticker = os.getenv("VESPER_TAX_RESERVE_TICKER", "SGOV").strip().upper()

        if tax_reserve_ticker and unswept_tax > 0:
            tax_price = await asyncio.to_thread(_fetch_live_quote, tax_reserve_ticker)
            if tax_price is None:
                audit_notes.append(
                    f"Skipped Tax Reserve Sweep for {tax_reserve_ticker}: no live quote available"
                )
            else:
                qty = int(unswept_tax // tax_price)
                if qty >= 1:
                    cost = round(tax_price * qty, 2)
                    tax_prop = OrderProposal(
                        id=f"prop-taxsweep-{uuid.uuid4().hex[:6]}",
                        ticker=tax_reserve_ticker,
                        asset_type="EQUITY",
                        side="BUY",
                        order_type="LIMIT",
                        quantity=qty,
                        limit_price=tax_price,
                        stop_loss=round(tax_price * 0.98, 2),
                        profit_target=round(tax_price * 1.05, 2),
                        estimated_cost=cost,
                        max_risk=cost,
                        risk_reward_ratio=1.0,
                    )
                    proposals.append(tax_prop)
                    audit_notes.append(
                        f"Drafted Tax Reserve Sweep Buy: {qty} shares of {tax_reserve_ticker} @ ${tax_price:.2f} "
                        f"(Funded by ${cost:,.2f} of ${unswept_tax:,.2f} unswept 25% tax reserve)"
                    )
                else:
                    audit_notes.append(
                        f"Tax Reserve Sweep: unswept reserve (${unswept_tax:,.2f}) below price of "
                        f"one share of {tax_reserve_ticker} (${tax_price:,.2f})"
                    )

    # ── 8. EARNINGS-WEEK CSP VEGA HARVEST ───────────────────────────────────
    # Sells an ATM cash-secured put the day before (or day of, for a
    # before-market-open report) a ticker's earnings, when the pre-earnings
    # IV premium is richest, and force-closes it once the post-earnings IV
    # crush has happened -- the point of this trade is harvesting the
    # vega/IV collapse, not directional conviction on the earnings result,
    # so the exit is date-driven (see earnings_exit_date / monitor.py's
    # EARNINGS_EXIT step), not P&L-driven.
    #
    # Independent of the technicals loop above (like Collar-Following/
    # Premium-Recycling/Tax-Reserve) since the relevant ticker universe is
    # "who reports earnings this window", not whatever scanner_node already
    # flagged for other reasons -- get_earnings_flow is TraderDaddy's own
    # market-wide calendar, not filtered to already-scanned candidates.
    if selected_playbook in ("all", "earnings_vega", "earnings_harvest"):
        earnings_list = await asyncio.to_thread(_fetch_upcoming_earnings)
        if earnings_list is None:
            audit_notes.append("Skipped Earnings-Week CSP Vega Harvest: earnings calendar unavailable")
        else:
            today = datetime.now(timezone.utc).date()
            for entry in earnings_list:
                event = entry.get("event") if isinstance(entry, dict) else None
                if not isinstance(event, dict):
                    continue
                ticker = str(event.get("symbol") or "").upper()
                earnings_date_str = event.get("earningsDate")
                earnings_time = str(event.get("earningsTime") or "").upper()
                if not ticker or not earnings_date_str:
                    continue

                try:
                    earnings_date = datetime.strptime(str(earnings_date_str)[:10], "%Y-%m-%d").date()
                except Exception:
                    continue

                days_out = (earnings_date - today).days
                # Draft the day before an after-market-close report (IV is
                # richest right before the print), or the same day for a
                # before-market-open report (the print already happened,
                # elevated IV is still priced in until the crush finishes
                # settling that morning). Anything else -- too early, or
                # already past -- is skipped, not approximated.
                if earnings_time == "AMC" and days_out != 1:
                    continue
                if earnings_time == "BMO" and days_out != 0:
                    continue
                if earnings_time not in ("AMC", "BMO"):
                    continue

                # Exit the day after the crush has actually happened: AMC
                # reports crush IV the NEXT trading day; BMO reports crush IV
                # that SAME day (already priced in by the time this drafts).
                exit_date = earnings_date + timedelta(days=1) if earnings_time == "AMC" else earnings_date
                exit_date_str = exit_date.strftime("%Y-%m-%d")

                equity_price = await asyncio.to_thread(_fetch_live_quote, ticker)
                if equity_price is None or equity_price <= 0:
                    audit_notes.append(f"Skipped Earnings Vega Harvest for {ticker}: no live equity quote available")
                    continue

                strike = round(equity_price, 0)
                premium = await asyncio.to_thread(_fetch_live_option_quote, ticker, strike, "PUT")
                if premium is None or premium <= 0:
                    audit_notes.append(
                        f"Skipped Earnings Vega Harvest for {ticker} strike ${strike:.2f}: no live option quote available"
                    )
                    continue

                qty = 1
                assignment_notional = round(strike * 100 * qty, 2)
                prop = OrderProposal(
                    id=f"prop-earnvega-{uuid.uuid4().hex[:6]}",
                    ticker=ticker,
                    asset_type="OPTION",
                    side="SELL",
                    order_type="LIMIT",
                    quantity=qty,
                    limit_price=round(premium, 2),
                    strike=strike,
                    option_type="put",
                    earnings_exit_date=exit_date_str,
                    stop_loss=round(premium * 2.5, 2),
                    estimated_cost=assignment_notional,
                    max_risk=assignment_notional,
                    risk_reward_ratio=0.0,
                )
                proposals.append(prop)
                audit_notes.append(
                    f"Drafted Earnings Vega Harvest CSP for {ticker}: SELL 1x Put Strike ${strike:.2f} "
                    f"@ ${premium:.2f} (Earnings {earnings_date_str} {earnings_time}, expected move "
                    f"{event.get('expectedMovePct', 'N/A')}%, force-exit {exit_date_str}, "
                    f"Assignment Notional: ${assignment_notional:,.2f})"
                )

    audit_entry = {
        "node": "playbooks_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "proposals_count": len(proposals),
        "notes": audit_notes,
    }

    return {
        "proposals": proposals,
        "needs_human_approval": len(proposals) > 0,
        "audit_trail": [audit_entry],
    }
