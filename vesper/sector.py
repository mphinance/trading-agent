"""Ticker -> GICS sector lookup, for the sector-concentration risk bucket.

Kept out of vesper/risk.py deliberately -- risk.py's own docstring convention
is a pure/no-I/O module (RiskEnforcer's methods take caller-supplied numbers,
never fetch anything themselves), and a network-backed sector lookup is I/O.
This module is the one place that talks to yfinance for sector classification;
RiskEnforcer.check_sector_concentration takes the resolved sector as a plain
argument, same as it takes position counts/notional for the other buckets.

Sector classification is metadata, not market data -- unlike a price or an
IV, a company's GICS sector does not change intraday (and rarely changes at
all). Caching it for the life of the process is the right call, not a rule-1
"don't fabricate market data" violation: nothing here is ever substituted for
a live value, a lookup that fails just returns None and the caller must skip,
never assume a default sector.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Module-level, in-process only. Never expires within a process -- only a
# restart re-fetches. Caching a None result too is deliberate: it stops
# risk_gate_node from re-hitting the network every single pass for a ticker
# that simply has no sector classification (crypto, some ETFs, delisted
# symbols) -- see get_sector's docstring.
_SECTOR_CACHE: Dict[str, Optional[str]] = {}


def get_sector(ticker: str) -> Optional[str]:
    """Resolve a ticker to its GICS sector via yfinance. Never raises.

    Returns None if the ticker is empty, unresolvable, or the lookup fails
    for any reason -- the caller must treat None as "unknown" and skip/fail
    closed, never assume a default sector (rule 1/2: no substituting a guess
    for real data, and guard logic fails closed on missing data).
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None

    if ticker in _SECTOR_CACHE:
        return _SECTOR_CACHE[ticker]

    try:
        import yfinance as yf

        sector = yf.Ticker(ticker).info.get("sector") or None
    except Exception as e:
        logger.warning(f"Sector lookup failed for {ticker}: {e}")
        sector = None

    _SECTOR_CACHE[ticker] = sector
    return sector
