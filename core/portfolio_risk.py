"""Household leverage, concentration and short-put assignment-collateral checks.

Ported from the Robinhood research engine. Vesper's
risk gate looks at one order at a time; this looks at the whole book: how levered
the account really is, how much of NET equity one name is, and what happens to
both if every ITM short put is assigned against the buying power on hand.

Pure functions. No I/O, no network, no clock except the generated_at stamp.
Input to build():
    equity_value, cash (negative = margin debit), options_value, buying_power,
    positions [{symbol, shares, spot}], short_puts [{symbol, strike, spot,
    mark, dte, contracts}], optional max_account_leverage_x.
"""

from __future__ import annotations

from datetime import datetime, timezone

CONCENTRATION_FLAG_PCT = 25.0


def leverage(equity_value, cash, options_value=0.0):
    """cash below zero is a margin debit, not spendable cash.

    net_equity is what is actually owned. gross_exposure is what is controlled.
    """
    debit = -cash if cash < 0 else 0.0
    net_equity = equity_value + cash + options_value
    gross = equity_value
    return {
        "gross_exposure_usd": gross,
        "margin_debit_usd": debit,
        "net_equity_usd": net_equity,
        "leverage_x": (gross / net_equity) if net_equity > 0 else None,
        "equity_ratio_pct": (net_equity / gross * 100.0) if gross > 0 else None,
        "financed_pct": (debit / gross * 100.0) if gross > 0 else 0.0,
    }


def post_assignment_leverage(short_puts, equity_value, cash, options_value):
    """Leverage if every ITM short put is assigned at expiry.

    Asked for on 2026-08-23 and deferred by three consecutive cycles, which is
    exactly how a known gap becomes a surprise. On 2026-08-26 all six short puts
    went ITM at once against $3,818 of buying power, so the answer stopped being
    hypothetical.

    Assignment buys the shares AT THE STRIKE and extinguishes the put. Net equity
    therefore RISES by the put's remaining time value -- you were short it -- so
    the danger is not the equity line, it is that gross exposure and the margin
    debit both grow by the full strike notional while net equity barely moves.
    Only ITM puts are counted: an OTM put expires worthless and assigns nothing.

    Delegates the arithmetic to pro_forma() rather than repeating it. All this
    adds is deriving the event list -- "every ITM put assigns" -- which is the
    only part that was actually missing.
    """
    itm = [p for p in short_puts if p["spot"] < p["strike"]]

    # The put's own mark is the liability that assignment extinguishes, and it
    # is the term that decides whether net equity rises or falls. Defaulting it
    # to 0.0 does not degrade the answer, it inverts it: on 2026-08-26 close a
    # payload built without marks reported 2.31x and net equity FALLING $4,760,
    # when the true figures are 2.09x and net equity RISING $1,068. Same rule as
    # cash_usd in mark_shadow (P15) -- refuse rather than guess.
    missing = [p["symbol"] for p in itm if p.get("mark") is None]
    if missing:
        raise ValueError(
            "short_puts missing 'mark' for {}: the put mark is the liability "
            "assignment extinguishes, so omitting it inverts the sign of the "
            "net-equity effect. Supply the mark; it is not inferable here."
            .format(", ".join(missing)))

    events = [{"symbol": p["symbol"], "action": "put_assigned",
               "strike": p["strike"], "contracts": p.get("contracts", 1),
               "mark": p["spot"],   # shares arrive worth SPOT, not the strike
               "option_liability_usd": p["mark"] * 100.0
               * p.get("contracts", 1)}
              for p in itm]
    out = pro_forma({"equity_value": equity_value, "cash": cash,
                     "options_value": options_value}, events)
    out["itm_puts"] = len(itm)
    out["assignment_notional_usd"] = sum(
        p["strike"] * 100.0 * p.get("contracts", 1) for p in itm)
    out["shares_received_mv_usd"] = sum(
        p["spot"] * 100.0 * p.get("contracts", 1) for p in itm)
    return out


def concentration(positions, net_equity, gross_exposure):
    """Per-name weight against both gross and net equity.

    Against net equity is the number that matters on a levered account and the
    one that looks alarming, which is why it is reported rather than buried.
    """
    rows = []
    for p in positions:
        mv = p["shares"] * p["spot"]
        rows.append({
            "symbol": p["symbol"],
            "market_value_usd": mv,
            "pct_of_gross": (mv / gross_exposure * 100.0) if gross_exposure else 0.0,
            "pct_of_net_equity": (mv / net_equity * 100.0) if net_equity > 0 else None,
            "flagged": (mv / gross_exposure * 100.0) > CONCENTRATION_FLAG_PCT
            if gross_exposure else False,
        })
    return sorted(rows, key=lambda r: r["market_value_usd"], reverse=True)


def assignment_collateral(short_puts, buying_power):
    """Short-put collateral status.

    Robinhood reserves the collateral when the put is OPENED, so for a put with
    `collateral_reserved` true the money is already set aside and the reported
    buying power is what remains AFTER it. Counting such a put against buying
    power double-counts it and invents a shortfall that does not exist.

    Only puts explicitly marked `collateral_reserved: false` can create a real
    funding gap.
    """
    rows, reserved, unreserved = [], 0.0, 0.0
    for p in short_puts:
        need = p["strike"] * 100.0 * p.get("contracts", 1)
        is_reserved = p.get("collateral_reserved", True)
        if is_reserved:
            reserved += need
        else:
            unreserved += need
        rows.append({
            "symbol": p["symbol"],
            "strike": p["strike"],
            "dte": p["dte"],
            "itm": p["spot"] < p["strike"],
            "capital_required_usd": need,
            "collateral_reserved": is_reserved,
        })
    rows.sort(key=lambda r: r["dte"])
    return {
        "puts": rows,
        "total_required_usd": reserved + unreserved,
        "already_reserved_usd": reserved,
        "unreserved_usd": unreserved,
        "buying_power_usd": buying_power,
        "shortfall_usd": max(0.0, unreserved - buying_power),
        "covered_by_buying_power": unreserved <= buying_power,
    }


def pro_forma(base, events):
    """Where leverage lands if expected assignments happen.

    events: [{"symbol", "action": "called_away"|"put_assigned", "strike",
              "shares"|"contracts", "market_value_usd", "option_liability_usd"}]

    Called away: shares leave, proceeds pay down the debit.
    Put assigned: shares arrive, reserved collateral converts into stock.
    """
    equity = base["equity_value"]
    cash = base["cash"]
    options = base.get("options_value", 0.0)
    steps = []

    for e in events:
        if e["action"] == "called_away":
            proceeds = e["strike"] * e["shares"]
            equity -= e["market_value_usd"]
            cash += proceeds
            options += e.get("option_liability_usd", 0.0)
            steps.append(f"{e['symbol']} called away at {e['strike']:.2f}, "
                         f"+${proceeds:,.0f} against the debit")
        elif e["action"] == "put_assigned":
            # Cash always leaves at the STRIKE. The shares arriving are worth
            # spot, and a put only assigns when spot is below the strike, so
            # marking them at the strike overstates gross exposure and makes
            # leverage read better than it is. Pass `mark` (the SHARE price) to
            # price them honestly; absent it we fall back to the strike and the
            # old optimistic behaviour.
            n = 100.0 * e.get("contracts", 1)
            cost = e["strike"] * n
            equity += e.get("mark", e["strike"]) * n
            cash -= cost
            options += e.get("option_liability_usd", 0.0)
            steps.append(f"{e['symbol']} put assigned at {e['strike']:.2f}, "
                         f"${cost:,.0f} of reserved cash becomes stock")

    lev = leverage(equity, cash, options)
    lev["steps"] = steps
    return lev


def build(payload):
    lev = leverage(
        payload["equity_value"], payload["cash"], payload.get("options_value", 0.0)
    )
    conc = concentration(
        payload["positions"], lev["net_equity_usd"], lev["gross_exposure_usd"]
    )
    coll = assignment_collateral(
        payload.get("short_puts", []), payload["buying_power"]
    )
    post = post_assignment_leverage(
        payload.get("short_puts", []), payload["equity_value"], payload["cash"],
        payload.get("options_value", 0.0)
    )

    warnings = []
    max_lev = payload.get("max_account_leverage_x")
    if post["itm_puts"] and post["leverage_x"] and max_lev and \
            post["leverage_x"] > max_lev:
        warnings.append(
            f"{post['itm_puts']} short puts are ITM. If all are assigned, "
            f"leverage goes {lev['leverage_x']:.2f}x -> {post['leverage_x']:.2f}x, "
            f"through the {max_lev:g}x max_account_leverage_x guard, on "
            f"${post['assignment_notional_usd']:,.0f} of stock against "
            f"${coll['buying_power_usd']:,.0f} of buying power."
        )
    if lev["leverage_x"] and lev["leverage_x"] > 1.5:
        warnings.append(
            f"Account is {lev['leverage_x']:.2f}x levered. "
            f"{lev['financed_pct']:.0f}% of the book is financed with a "
            f"${lev['margin_debit_usd']:,.0f} margin debit that accrues interest "
            f"daily and does not shrink when the book falls."
        )
    for c in conc:
        if c["flagged"]:
            warnings.append(
                f"{c['symbol']} is {c['pct_of_gross']:.1f}% of gross exposure and "
                f"{c['pct_of_net_equity']:.1f}% of NET equity. Concentration "
                f"threshold is {CONCENTRATION_FLAG_PCT:.0f}% of gross."
            )
    if coll["unreserved_usd"] > 0 and not coll["covered_by_buying_power"]:
        warnings.append(
            f"Short puts with UNRESERVED collateral require "
            f"${coll['unreserved_usd']:,.0f} against "
            f"${coll['buying_power_usd']:,.0f} buying power. Shortfall "
            f"${coll['shortfall_usd']:,.0f}, which would deepen the margin debit."
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account": payload.get("account"),
        "leverage": lev,
        "concentration": conc,
        "assignment_collateral": coll,
        "post_assignment": post,
        "warnings": warnings,
    }
