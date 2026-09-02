"""Interactive Runner for LangGraph Quant Trading Agent."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
import logging
from typing import Optional, Dict, Any

from core.metrics import metrics
from vesper.state import TradingState
from vesper.graph import build_trading_graph

logger = logging.getLogger(__name__)


async def run_agent_session(
    mode: str = "dry_run",
    playbook: str = "all",
    target_ticker: Optional[str] = None,
    interactive: bool = True,
    persona: str = "default",
) -> Dict[str, Any]:
    """Runs a complete trading session through the LangGraph StateGraph."""
    session_id = f"vesper-{uuid.uuid4().hex[:8]}"
    print("=" * 72)
    print(f"⚡ VESPER QUANTITATIVE EXECUTION ENGINE | Session: {session_id}")
    print(
        f"Mode: {mode.upper()} | Playbook: {playbook.upper()} | "
        f"Persona: {persona.upper()} | Target: {target_ticker or 'AUTO-DISCOVERY'}"
    )
    print("=" * 72)

    # Optional Whop Licensing Check (Commercial Mode)
    import os
    from vesper.whop import WhopClient
    whop_license = os.getenv("WHOP_LICENSE_KEY")
    if whop_license:
        with WhopClient() as whop:
            val_res = whop.validate_license(whop_license)
            if val_res.get("valid"):
                print(f"🔑 Whop License Validated: {val_res.get('email', 'Active Member')}")
            else:
                print(f"⚠️ Whop License Warning: {val_res.get('reason')}")

    app = await build_trading_graph(checkpointer=True)

    # Register this compiled graph with the ApprovalRegistry so an inbound
    # Telegram/Discord/webhook decision (see vesper/bot/inbound.py) can
    # resume it via Command(resume=...) while this session is paused at
    # human_gate_node. Harmless to call repeatedly -- it just overwrites the
    # registry's single graph_app pointer with the currently-running one.
    from vesper.bot.inbound import approval_registry
    approval_registry.set_graph_app(app)

    config = {"configurable": {"thread_id": session_id}}

    initial_state: TradingState = {
        "session_id": session_id,
        "mode": mode,
        "selected_playbook": playbook,
        "target_ticker": target_ticker,
        "persona": persona,
        "regime": None,
        "candidates": [],
        "technicals": {},
        "options_audits": {},
        "proposals": [],
        "rejected_proposals": [],
        "execution_results": [],
        "needs_human_approval": False,
        "human_decision": None if interactive else "AUTO_DRY_RUN",
        "audit_trail": [],
        "reflection_notes": [],
        "errors": [],
    }

    final_state = None
    async for event in app.astream(initial_state, config=config):
        for node_name, output in event.items():
            print(f"\n[✓] Finished Node: {node_name}")
            
            # Print node-specific highlights
            if node_name == "regime_node" and "regime" in output:
                reg = output["regime"]
                print(f"    Posture: {reg.posture} | Health: {reg.health_label} ({reg.health_score}/7.0)")
                if reg.spy_spot and reg.spy_gamma_flip:
                    print(f"    SPY: ${reg.spy_spot:.2f} | Gamma Flip: ${reg.spy_gamma_flip:.2f} ({reg.spy_gex_regime})")

            elif node_name == "scanner_node" and "candidates" in output:
                cands = output["candidates"]
                print(f"    Discovered {len(cands)} candidate(s): {', '.join(c.ticker for c in cands)}")

            elif node_name == "analyst_node" and "technicals" in output:
                for t, tech in output["technicals"].items():
                    print(f"    {t}: Close=${tech.close:.2f} | RSI={tech.rsi_14:.1f} | EMA Stack={tech.ema_stack}")

            elif node_name == "playbooks_node" and "proposals" in output:
                props = output["proposals"]
                print(f"    Drafted {len(props)} proposal(s):")
                for p in props:
                    print(f"     • [{p.id}] {p.side} {p.quantity}x {p.ticker} ({p.asset_type}) @ ${p.limit_price:.2f} (Est Cost: ${p.estimated_cost:,.2f})")

            elif node_name == "risk_gate_node":
                passed = len(output.get("proposals", []))
                rejected = len(output.get("rejected_proposals", []))
                metrics.record_tool_rejection("risk_gate_node", passed=passed, rejected=rejected)

            elif node_name == "executor_node" and "execution_results" in output:
                broker = os.getenv("EXECUTION_BROKER", "webull")
                for res in output["execution_results"]:
                    print(f"    Execution: [{res.status}] {res.ticker} - {res.message}")
                    digest = hashlib.sha256(f"{res.order_proposal_id}:{res.ticker}".encode()).hexdigest()[:16]
                    metrics.record_order_outcome(mode=mode, status=res.status, broker=broker, payload_digest=digest)

            elif node_name == "reflection_node" and "reflection_notes" in output:
                for note in output["reflection_notes"]:
                    print(f"    Journal: {note}")

            final_state = output

    print("\n" + "=" * 70)
    print("✓ SESSION COMPLETE")
    print("=" * 70)
    return final_state
