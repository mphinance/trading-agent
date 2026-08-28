"""Execution Engine Node (Webull OpenAPI & Dry-Run Simulation)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from vesper.state import TradingState, ExecutionResult

logger = logging.getLogger(__name__)


async def executor_node(state: TradingState) -> Dict[str, Any]:
    """Executes approved orders on Webull or simulates dry-run fills."""
    logger.info("-> [ExecutorNode] Processing execution queue...")
    
    proposals = state.get("proposals", [])
    results: List[ExecutionResult] = []
    audit_notes = []
    mode = state.get("mode", "dry_run")

    for prop in proposals:
        if not prop.approved:
            res = ExecutionResult(
                order_proposal_id=prop.id,
                ticker=prop.ticker,
                status="REJECTED_BY_USER",
                message=f"Order proposal {prop.id} was not approved.",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            results.append(res)
            audit_notes.append(f"Skipped {prop.id}: Not approved.")
            continue

        if mode == "dry_run" or state.get("human_decision") == "AUTO_DRY_RUN":
            # Simulated Fill
            res = ExecutionResult(
                order_proposal_id=prop.id,
                ticker=prop.ticker,
                status="DRY_RUN_SIMULATED",
                client_order_id=f"sim-{prop.id}",
                filled_quantity=prop.quantity,
                filled_price=prop.limit_price,
                fees=0.0,
                message=f"Simulated {prop.side} {prop.quantity} {prop.ticker} @ ${prop.limit_price:.2f}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            results.append(res)
            audit_notes.append(f"DRY RUN FILLED: {prop.side} {prop.quantity}x {prop.ticker} @ ${prop.limit_price:.2f}")
        else:
            import os
            broker_target = os.getenv("EXECUTION_BROKER", "webull").lower()
            if broker_target == "public":
                try:
                    from vesper.brokers.public_broker import PublicBrokerClient
                    with PublicBrokerClient() as pub:
                        if not pub.configured:
                            raise RuntimeError("Public.com API key (PUBLIC_API_SECRET_KEY) not configured in .env")
                        order_res = pub.place_order(
                            symbol=prop.ticker,
                            side=prop.side,
                            quantity=prop.quantity,
                            order_type=prop.order_type,
                            limit_price=prop.limit_price,
                        )
                    res = ExecutionResult(
                        order_proposal_id=prop.id,
                        ticker=prop.ticker,
                        status="SUBMITTED",
                        client_order_id=f"pub-{prop.id}",
                        filled_quantity=prop.quantity,
                        filled_price=prop.limit_price,
                        message=f"Order submitted to Public.com: {order_res.get('data', 'OK')}",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                    results.append(res)
                    audit_notes.append(f"PUBLIC.COM SUBMITTED: {prop.side} {prop.quantity}x {prop.ticker} @ ${prop.limit_price:.2f}")
                except Exception as e:
                    logger.error(f"Public.com execution failed for {prop.id}: {e}")
                    res = ExecutionResult(
                        order_proposal_id=prop.id,
                        ticker=prop.ticker,
                        status="FAILED",
                        message=str(e),
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                    results.append(res)
                    audit_notes.append(f"EXECUTION FAILED: {prop.id} - {e}")
            else:
                # Live Webull Order Execution
                try:
                    from wb import Webull
                    wb = Webull()
                    if not wb.configured:
                        raise RuntimeError("Webull client not configured in .env")
                    
                    # Fetch cash account
                    acc_list = wb.trade.account_v2.get_account_list()
                    accounts = acc_list.get("data", [])
                    account_id = None
                    for acc in accounts:
                        if acc.get("account_class") == "INDIVIDUAL_CASH":
                            account_id = acc.get("account_id")
                            break
                            
                    if not account_id and accounts:
                        account_id = accounts[0].get("account_id")

                    order_payload = {
                        "account_id": account_id,
                        "symbol": prop.ticker,
                        "side": prop.side,
                        "order_type": prop.order_type,
                        "limit_price": prop.limit_price,
                        "quantity": prop.quantity,
                        "time_in_force": "DAY",
                    }

                    # NOTE: this intentionally previews only and does not call
                    # place_order. The notional cap / confirm handshake / kill
                    # switch that used to guard live Webull orders (orders.py)
                    # were removed in the sidecar->Vesper migration and have not
                    # been rebuilt (see ROADMAP.md Phase 0). Reporting a preview
                    # as "SUBMITTED" would be a false positive, so this branch is
                    # blocked until guards land rather than silently going live.
                    preview_res = wb.trade.order_v2.preview_order(order_payload)
                    logger.warning(
                        f"Webull live execution is blocked pending guardrails (see ROADMAP.md Phase 0); "
                        f"order for {prop.id} was only previewed, not placed."
                    )

                    res = ExecutionResult(
                        order_proposal_id=prop.id,
                        ticker=prop.ticker,
                        status="BLOCKED_PENDING_GUARDRAILS",
                        client_order_id=f"wb-{prop.id}",
                        message=(
                            f"Webull order NOT placed: live execution guardrails are not yet "
                            f"rebuilt (ROADMAP.md Phase 0). Preview only: {preview_res.get('data', 'OK')}"
                        ),
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                    results.append(res)
                    audit_notes.append(f"BLOCKED (guardrails missing): {prop.side} {prop.quantity}x {prop.ticker} @ ${prop.limit_price:.2f} — previewed only")
                except Exception as e:
                    logger.error(f"Live execution failed for {prop.id}: {e}")
                    res = ExecutionResult(
                        order_proposal_id=prop.id,
                        ticker=prop.ticker,
                        status="FAILED",
                        message=str(e),
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                    results.append(res)
                    audit_notes.append(f"EXECUTION FAILED: {prop.id} - {e}")

    audit_entry = {
        "node": "executor_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "executed_count": len(results),
        "notes": audit_notes,
    }

    return {
        "execution_results": results,
        "audit_trail": [audit_entry],
    }
