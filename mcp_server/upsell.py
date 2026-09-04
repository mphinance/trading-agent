"""Make the paid layer visible from inside the free one.

The free tools (screeners, technicals, backtests, EDGAR) are a complete
research product on their own — that is deliberate, and it costs nothing to
give away, because an MCP server runs on the user's machine and these tools call
yfinance / TradingView / EDGAR from there. Zero TMpro API calls, zero rate limit.

The risk that creates is the whole reason this module exists: someone installs
for a VCP screener, gets a VCP screener, and never learns that dealer gamma,
options flow and apex levels exist at all. A free product that is silently
complete has no funnel — the paid layer is not walled off, it is *invisible*.

So a screener that just returned eight tickers ends with one line saying what it
cannot tell you about them. Three rules, all of which are the difference between
a hint and a nag:

1. **Only when no key is configured.** Someone who already pays never sees it.
2. **One line, once, in its own field.** It goes in `note`, never mixed into the
   data, so a caller parsing results is unaffected and a model reading the
   result can mention it or not.
3. **It says what is missing, not how great we are.** "Dealer gamma and options
   flow for these names need a key" is useful. Marketing copy in a tool response
   trains people to ignore tool responses.
"""

from __future__ import annotations

import os
from typing import Any

SIGNUP_URL = "https://traderdaddy.pro"

# What the paid layer would add, per kind of free result. Keyed by the shape of
# the question the user just asked, because a hint that does not follow from the
# result they are looking at is an advert.
_HINTS: dict[str, str] = {
    "screen": (
        "Dealer gamma, unusual options flow and apex levels for these names are "
        "not included in the free tools"
    ),
    "technicals": (
        "This is price structure only — dealer positioning (gamma walls, flip, "
        "pins) for this name is not included in the free tools"
    ),
    "backtest": (
        "Backtested on price alone. Options flow and dealer positioning at each "
        "entry are not included in the free tools"
    ),
}


def _has_tmpro_key() -> bool:
    """True when a TraderMatrix Pro key is configured, by either name."""
    return bool((os.getenv("TD_API_KEY") or os.getenv("TDPRO_API_KEY") or "").strip())


def free_tier_note(kind: str, *, count: int | None = None) -> str | None:
    """One line naming what the free tier could not answer, or None.

    Returns None when a key is configured (they already pay) or when `kind` is
    not one we have an honest hint for — inventing one for an unmapped tool is
    how this becomes noise.
    """
    if _has_tmpro_key():
        return None

    hint = _HINTS.get(kind)
    if hint is None:
        return None

    subject = f"{count} results" if count else "These results"
    return f"{subject}: {hint}. See {SIGNUP_URL}"


def with_free_tier_note(result: Any, kind: str, *, count: int | None = None) -> Any:
    """Attach `note` to a dict result, leaving everything else untouched.

    Non-dict results pass straight through: a tool that returns a list or a
    string is not worth reshaping for a hint, and changing its type to carry one
    would break callers.
    """
    note = free_tier_note(kind, count=count)
    if note is None or not isinstance(result, dict):
        return result

    # Never clobber a note the tool itself set — its own message is about the
    # data and matters more than ours.
    if result.get("note"):
        return result

    return {**result, "note": note}
