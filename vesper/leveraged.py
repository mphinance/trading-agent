"""Leveraged ETF Mappings and High-Beta Instrument Lookup."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Dict, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "leveraged_etfs.db"


def get_leveraged_etfs(underlying: str) -> List[Dict[str, str]]:
    """Lookup leveraged/inverse ETF vehicles for an underlying stock symbol.
    
    Args:
        underlying: Stock ticker (e.g. 'NVDA', 'TSLA', 'MSTR', 'AVGO')
        
    Returns:
        List of dicts with 'etf_ticker', 'provider', and 'company_name'.
    """
    if not DB_PATH.exists():
        return []

    ticker = underlying.strip().upper()
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT etf_ticker, provider, company_name FROM mappings WHERE underlying = ?",
            (ticker,),
        )
        rows = cursor.fetchall()
        return [
            {"etf_ticker": r[0], "provider": r[1], "company_name": r[2]}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_primary_2x(underlying: str) -> Optional[str]:
    """Get the primary 2x bull ETF for a ticker if available."""
    etfs = get_leveraged_etfs(underlying)
    if etfs:
        return etfs[0]["etf_ticker"]
    return None
