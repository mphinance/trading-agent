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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from vesper.execution_guard import GuardError, TradingDisabled, guard
from vesper.state import ExecutionResult, OrderProposal, TradingState

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
    from wb import Webull

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

    place_res = await asyncio.to_thread(
        guard.place, ticket.id, payload, lambda: wb.trade.order_v2.place_order(payload)
    )

    return ExecutionResult(
        order_proposal_id=prop.id,
        ticker=prop.ticker,
        status="SUBMITTED",
        client_order_id=f"wb-{prop.id}",
        message=f"Order placed on Webull: {place_res.get('data', place_res) if isinstance(place_res, dict) else place_res}",
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
