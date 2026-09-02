"""Execution Engine Node (Webull OpenAPI, Public.com & Dry-Run Simulation).

Live orders go through vesper.execution_guard.guard (preview -> ticket ->
place), which enforces the kill switch (VESPER_TRADING, default off),
notional/quantity caps, and an optional symbol allowlist before any broker
call happens. See docs/CODE_SWEEP_2026-08-28.md for why this exists — the
sidecar->Vesper migration deleted the equivalent guards in orders.py without
rebuilding them, and this module used to place/preview orders with none of
that in place.

Every blocking SDK/HTTP call runs inside asyncio.to_thread: this node is
async (it's a LangGraph node), but wb.py's Webull client and PublicBrokerClient
are both synchronous, and calling them inline would stall the event loop for
the duration of the network round-trip.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from vesper.execution_guard import GuardError, TradingDisabled, guard
from vesper.state import ExecutionResult, OrderProposal, TradingState

# Namespace for deriving a stable client_order_id from a proposal id. Webull
# dedupes on client_order_id, so deriving it deterministically (rather than a
# fresh uuid4 per attempt) means a retry of the SAME proposal cannot become a
# second live order.
_CLIENT_ORDER_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _client_order_id(proposal_id: str) -> str:
    return uuid.uuid5(_CLIENT_ORDER_NS, proposal_id).hex


def _webull_equity_order(prop: OrderProposal, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a guard payload into Webull's single-leg equity wire format.

    Shape verified live 2026-08-29 against preview_order, which accepted it and
    returned a cost estimate -- see docs/WEBULL_ORDER_PAYLOADS.md. The previous
    version of this call was wrong three ways: it passed one dict positionally
    (the real signature is place_order(account_id, new_orders, ...) with a
    LIST), it sent `asset_type` where Webull wants `instrument_type`, and it
    omitted combo_type / market / entrust_type / support_trading_session /
    client_order_id, each of which Webull rejects the request without.

    Prices and quantities go over the wire as STRINGS.
    """
    return {
        "client_order_id": _client_order_id(prop.id),
        "combo_type": "NORMAL",
        "instrument_type": payload["asset_type"],
        "market": "US",
        "symbol": payload["symbol"],
        "side": payload["side"],
        "order_type": payload["order_type"],
        "limit_price": f"{float(payload['limit_price']):.2f}",
        "quantity": str(int(payload["quantity"])),
        "time_in_force": payload.get("time_in_force", "DAY"),
        "entrust_type": "QTY",
        "support_trading_session": "N",
    }

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def executor_node(state: TradingState) -> Dict[str, Any]:
    """Executes approved orders on Webull/Public.com or simulates dry-run fills."""
    logger.info("-> [ExecutorNode] Processing execution queue...")

    proposals = state.get("proposals", [])
    results: List[ExecutionResult] = []
    audit_notes: List[str] = []
    mode = state.get("mode", "dry_run")

    for prop in proposals:
        if not prop.approved:
            results.append(
                ExecutionResult(
                    order_proposal_id=prop.id,
                    ticker=prop.ticker,
                    status="REJECTED_BY_USER",
                    message=f"Order proposal {prop.id} was not approved.",
                    timestamp=_now(),
                )
            )
            audit_notes.append(f"Skipped {prop.id}: Not approved.")
            continue

        if mode == "dry_run" or state.get("human_decision") == "AUTO_DRY_RUN":
            # The dry-run path never touches execution_guard (no broker call to
            # guard), which also meant it never saw the guard's halt check --
            # so a resume landing while halted still wrote a paper fill. No
            # money moves, but a halt should mean STOP, and the paper ledger it
            # writes to is what circuit_breaker reads NLV from, so a fill
            # recorded during a freeze feeds back into the breaker's own
            # drawdown maths. Checked explicitly here rather than by routing
            # dry-run through the guard, which would require inventing a
            # broker-less ticket for something that places no order.
            from vesper.halt import is_halted

            halted, halt_info = is_halted()
            if halted:
                reason = (halt_info or {}).get("reason", "halted")
                results.append(
                    ExecutionResult(
                        order_proposal_id=prop.id,
                        ticker=prop.ticker,
                        status="BLOCKED_BY_GUARDRAIL",
                        message=f"Vesper is HALTED: {reason}",
                        timestamp=_now(),
                    )
                )
                audit_notes.append(f"BLOCKED {prop.id}: halted — no paper fill recorded")
                continue

            sim_res = ExecutionResult(
                order_proposal_id=prop.id,
                ticker=prop.ticker,
                status="DRY_RUN_SIMULATED",
                client_order_id=f"sim-{prop.id}",
                filled_quantity=prop.quantity,
                filled_price=prop.limit_price,
                fees=0.0,
                message=f"Simulated {prop.side} {prop.quantity} {prop.ticker} @ ${prop.limit_price:.2f}",
                timestamp=_now(),
            )
            results.append(sim_res)

            # Record in Paper Trading Ledger (Module 7)
            try:
                from vesper.paper_ledger import record_paper_fill
                record_paper_fill(proposal=prop, result=sim_res, session_id=state.get("session_id"))
            except Exception as e:
                logger.warning(f"Failed to record paper fill for {prop.ticker}: {e}")

            audit_notes.append(
                f"DRY RUN FILLED: {prop.side} {prop.quantity}x {prop.ticker} @ ${prop.limit_price:.2f}"
            )
            continue

        broker_target = os.getenv("EXECUTION_BROKER", "webull").lower()
        try:
            if broker_target == "public":
                res = await _execute_public(prop)
            else:
                res = await _execute_webull(prop)
            results.append(res)
            audit_notes.append(
                f"{res.status}: {prop.side} {prop.quantity}x {prop.ticker} @ ${prop.limit_price:.2f}"
            )
        except (GuardError, TradingDisabled) as e:
            logger.warning(f"Execution blocked by guard for {prop.id}: {e}")
            results.append(
                ExecutionResult(
                    order_proposal_id=prop.id,
                    ticker=prop.ticker,
                    status="BLOCKED_BY_GUARDRAIL",
                    message=str(e),
                    timestamp=_now(),
                )
            )
            audit_notes.append(f"BLOCKED {prop.id}: {e}")
        except Exception as e:
            logger.error(f"Execution failed for {prop.id}: {e}")
            results.append(
                ExecutionResult(
                    order_proposal_id=prop.id,
                    ticker=prop.ticker,
                    status="FAILED",
                    message=str(e),
                    timestamp=_now(),
                )
            )
            audit_notes.append(f"EXECUTION FAILED: {prop.id} - {e}")

    # Broadcast execution results to active channels (Telegram/Discord/Webhook)
    from vesper.bot.manager import channel_manager
    if channel_manager.active_channels:
        for res in results:
            await channel_manager.broadcast_execution(res)

    audit_entry = {
        "node": "executor_node",
        "timestamp": _now(),
        "executed_count": len(results),
        "notes": audit_notes,
    }

    return {
        "execution_results": results,
        "audit_trail": [audit_entry],
    }


async def _execute_webull(prop: OrderProposal) -> ExecutionResult:
    if prop.legs:
        return await _execute_webull_multileg(prop)

    from core.wb import Webull

    def _fetch_account_and_place(payload: Optional[dict] = None):
        """Runs entirely on a worker thread: constructs the (cheap, local)
        client, then does the blocking account-list + buying-power reads."""
        wb = Webull()
        if not wb.configured:
            raise RuntimeError("Webull client not configured in .env")

        accounts = wb.trade.account_v2.get_account_list().get("data", [])
        account_id = next(
            (a.get("account_id") for a in accounts if a.get("account_class") == "INDIVIDUAL_CASH"),
            None,
        )
        if not account_id and accounts:
            account_id = accounts[0].get("account_id")

        try:
            buying_power = wb.portfolio()["totals"]["buying_power"]
        except Exception:
            buying_power = None

        return wb, account_id, buying_power

    wb, account_id, buying_power = await asyncio.to_thread(_fetch_account_and_place)

    # The guard payload uses execution_guard's OWN field names (asset_type,
    # quantity, strike, is_closing). Webull's wire format uses different ones.
    # Keep them separate and derive the wire payload from this one inside
    # _place(), so the dict the ticket hashes stays the canonical description
    # of the order and the thing actually sent is a pure function of it --
    # there is no second place for the two to drift apart.
    payload = {
        "account_id": account_id,
        "symbol": prop.ticker,
        "side": prop.side,
        "order_type": prop.order_type,
        "limit_price": prop.limit_price,
        "quantity": prop.quantity,
        "asset_type": prop.asset_type,
        "time_in_force": "DAY",
        # execution_guard needs this for a SELL option's notional (strike,
        # not premium, is the real capital at risk) -- harmless for
        # everything else, guard only reads it on that one branch.
        "strike": prop.strike,
    }

    # Guards run before the ticket exists at all — TradingDisabled/GuardError
    # here means no broker call is made.
    ticket = guard.preview(prop.id, payload, live_buying_power=buying_power)

    def _place():
        return wb.trade.order_v2.place_order(account_id, [_webull_equity_order(prop, payload)])

    place_res = await asyncio.to_thread(guard.place, ticket.id, payload, _place)

    return ExecutionResult(
        order_proposal_id=prop.id,
        ticker=prop.ticker,
        status="SUBMITTED",
        client_order_id=f"wb-{prop.id}",
        message=f"Order placed on Webull: {place_res.get('data', place_res) if isinstance(place_res, dict) else place_res}",
        timestamp=_now(),
    )


async def _execute_webull_multileg(prop: OrderProposal) -> ExecutionResult:
    """Places a multi-leg combo order (e.g. SYNTHETIC_LONG) via Webull's
    place_option(account_id, new_orders, client_combo_order_id=...), where
    new_orders[0]["legs"] carries each leg.

    Leg wire format verified live 2026-08-29 (see docs/WEBULL_ORDER_PAYLOADS.md):
    a leg is identified by **underlying + strike_price + option_expire_date +
    option_type**, NOT by an options contract symbol, and `side` is required at
    BOTH the order and the leg level. This function previously sent
    `symbol: leg.contract_symbol`, which Webull rejects.

    What is verified is a SINGLE option leg (`option_strategy: "SINGLE"`,
    accepted by preview_option). Anything else is refused below rather than
    guessed at -- see the two checks.
    """
    from core.wb import Webull

    # Refusal 1: mixed equity+option combos (Thega's 100 shares + call + puts).
    # place_option is an options endpoint; whether it accepts an EQUITY leg at
    # all is unverified, and there is no evidence either way. Sending a
    # probably-wrong payload to a live order endpoint is exactly what the
    # "never fabricate" rule exists to stop, so refuse with a message that says
    # what would make it verifiable.
    if any(l.asset_type != "OPTION" for l in prop.legs):
        raise GuardError(
            f"{prop.strategy_type} for {prop.ticker} mixes EQUITY and OPTION legs. Webull's "
            "place_option leg schema is verified for OPTION legs only (see "
            "docs/WEBULL_ORDER_PAYLOADS.md); whether it accepts an equity leg is unverified. "
            "Refusing rather than sending a guessed payload to a live order endpoint."
        )

    # Refusal 2: multi-option-leg combos. `option_strategy` is a required field
    # and only SINGLE is confirmed accepted (VERTICAL is a valid enum value but
    # its full leg shape is unproven; SYNTHETIC_LONG -- long call + short put at
    # one strike -- has no confirmed enum value at all). Guessing the strategy
    # name on a live order is not worth the downside.
    option_legs = [l for l in prop.legs if l.asset_type == "OPTION"]
    if len(option_legs) != 1:
        raise GuardError(
            f"{prop.strategy_type} for {prop.ticker} needs {len(option_legs)} option legs, but only "
            "the single-leg option payload (option_strategy=SINGLE) is verified against Webull. "
            "The correct option_strategy value for this combo is unconfirmed -- refusing rather "
            "than guessing. See docs/WEBULL_ORDER_PAYLOADS.md."
        )

    for leg in option_legs:
        # A leg is routed by underlying+strike+expiry+type, so those must exist.
        # contract_symbol is deliberately NOT required any more: it is not what
        # Webull matches on.
        if leg.strike is None or not leg.expiry or not leg.option_type:
            raise GuardError(
                f"option leg for {prop.ticker} is missing strike/expiry/option_type "
                f"(strike={leg.strike}, expiry={leg.expiry}, type={leg.option_type}) -- "
                "these identify the contract to Webull; refusing to place an underspecified order"
            )

    def _fetch_account_and_place(payload: Optional[dict] = None):
        wb = Webull()
        if not wb.configured:
            raise RuntimeError("Webull client not configured in .env")

        accounts = wb.trade.account_v2.get_account_list().get("data", [])
        account_id = next(
            (a.get("account_id") for a in accounts if a.get("account_class") == "INDIVIDUAL_CASH"),
            None,
        )
        if not account_id and accounts:
            account_id = accounts[0].get("account_id")

        try:
            buying_power = wb.portfolio()["totals"]["buying_power"]
        except Exception:
            buying_power = None

        return wb, account_id, buying_power

    wb, account_id, buying_power = await asyncio.to_thread(_fetch_account_and_place)

    guard_payload = {
        "account_id": account_id,
        "symbol": prop.ticker,
        "asset_type": "OPTION",
        "strategy_type": prop.strategy_type,
        "legs": [
            {
                "side": leg.side,
                "asset_type": leg.asset_type,
                "option_type": leg.option_type,
                "strike": leg.strike,
                "expiry": leg.expiry,
                "quantity": leg.quantity,
                "limit_price": leg.limit_price,
                "contract_symbol": leg.contract_symbol,
            }
            for leg in prop.legs
        ],
    }

    ticket = guard.preview(prop.id, guard_payload, live_buying_power=buying_power)

    def _place():
        # Shape verified live against preview_option (docs/WEBULL_ORDER_PAYLOADS.md).
        # `side` appears at BOTH levels deliberately -- that is what Webull
        # requires, not a copy-paste error. `combo_type` is "NORMAL" (an order
        # envelope value); the strategy goes in `option_strategy`, which is a
        # different field with a different vocabulary -- conflating the two was
        # the original bug here.
        leg = option_legs[0]
        new_orders = [{
            "client_order_id": _client_order_id(prop.id),
            "combo_type": "NORMAL",
            "option_strategy": "SINGLE",
            "order_type": prop.order_type,
            "quantity": str(int(leg.quantity)),
            "limit_price": f"{float(leg.limit_price):.2f}",
            "side": leg.side,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "legs": [
                {
                    "side": leg.side,
                    "quantity": str(int(leg.quantity)),
                    # The UNDERLYING ticker, not a contract symbol.
                    "symbol": prop.ticker,
                    "strike_price": f"{float(leg.strike):g}",
                    "option_expire_date": str(leg.expiry)[:10],
                    "instrument_type": "OPTION",
                    "option_type": str(leg.option_type).upper(),
                    "market": "US",
                }
            ],
        }]
        return wb.trade.order_v2.place_option(account_id, new_orders)

    place_res = await asyncio.to_thread(guard.place, ticket.id, guard_payload, _place)

    return ExecutionResult(
        order_proposal_id=prop.id,
        ticker=prop.ticker,
        status="SUBMITTED",
        client_order_id=f"wb-combo-{prop.id}",
        message=f"Combo order placed on Webull: {place_res.get('data', place_res) if isinstance(place_res, dict) else place_res}",
        timestamp=_now(),
    )


async def _execute_public(prop: OrderProposal) -> ExecutionResult:
    from vesper.brokers.public_broker import PublicBrokerClient

    def _place():
        with PublicBrokerClient() as pub:
            if not pub.configured:
                raise RuntimeError("Public.com API key (PUBLIC_API_SECRET_KEY) not configured in .env")

            payload = {
                "symbol": prop.ticker,
                "side": prop.side,
                "quantity": prop.quantity,
                "order_type": prop.order_type,
                "limit_price": prop.limit_price,
                "asset_type": prop.asset_type,
                "strike": prop.strike,
            }

            # Fetch live buying power from Public portfolio if available
            buying_power = pub.get_buying_power()
            ticket = guard.preview(prop.id, payload, live_buying_power=buying_power)
            return guard.place(
                ticket.id,
                payload,
                lambda: pub.place_order(
                    symbol=payload["symbol"],
                    side=payload["side"],
                    quantity=payload["quantity"],
                    order_type=payload["order_type"],
                    limit_price=payload["limit_price"],
                ),
            )

    order_res = await asyncio.to_thread(_place)

    return ExecutionResult(
        order_proposal_id=prop.id,
        ticker=prop.ticker,
        status="SUBMITTED",
        client_order_id=f"pub-{prop.id}",
        message=f"Order submitted to Public.com: {order_res.get('data', order_res) if isinstance(order_res, dict) else order_res}",
        timestamp=_now(),
    )
