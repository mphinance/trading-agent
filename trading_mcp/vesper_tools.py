"""trading_mcp.vesper_tools: read-only MCP tools over Vesper's already-tested
internals.

Phase 0, read-only only (see CLAUDE.md rule 3 and the ROADMAP note this file
was built against): no tool here may call `execution_guard.preview()` /
`.place()`, `halt()` / `resume()`, or `ApprovalRegistry.submit_decision()`.
The order path stays exactly where it is -- `vesper/nodes/executor.py`, run
only after a Telegram/Discord button tap resumes a checkpointed graph
thread. Nothing in this file can move money, freeze/unfreeze trading, or
resolve a pending approval; it can only look at state other code already
produced.

Every tool below is a thin wrapper: it lazily imports (inside the function
body, matching `vesper/account.py`'s and `core/circuit_breaker.py`'s own
convention for cross-module deps -- keeps this module importable even if one
dependency, e.g. the Webull SDK or chromadb, is missing or misbehaving in a
given environment) one existing, already-tested function, calls it, and
reshapes the result into a plain dict. None of them raise. Every one
degrades to a structured `{"available": False, "reason": ...}` (or
equivalent) on failure so one broken data source can never take the whole
MCP server down.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def register_vesper_tools(mcp: Any) -> list[str]:
    """Register every read-only Vesper tool onto `mcp` and return their names.

    Mirrors `mcp_server/registry.py`'s `register_tierN_tools` shape: one
    `register_*` function, each tool defined as a closure with `@mcp.tool()`,
    an explicit name list at the end (rather than introspecting `mcp`) so the
    registered set is visible at a glance and stays in sync with the
    docstring above.
    """

    @mcp.tool()
    async def get_account_state() -> dict[str, Any]:
        """Live equity, buying power and open positions.

        Sourced from a single `wb.Webull().portfolio()` call so this
        inherits the SDK client's 2-req/2s lock, retry/backoff and
        stale-snapshot fallback (see wb.py's module docstring and
        CLAUDE.md's rate-limit gotcha). Deliberately does NOT also call
        `vesper/account.py`'s `fetch_live_equity()` /
        `fetch_live_buying_power()` on top of this: each of those builds
        its OWN Webull client and does its OWN `portfolio()` fetch, and
        stacking three independent fetches against the scarce order-query
        bucket inside a single tool call is exactly the kind of
        self-inflicted 429 that bucket's pacing exists to avoid. One
        fetch answers equity, buying power and positions together, from
        the same `totals` dict those two functions themselves read.
        """
        try:
            from core.wb import Webull, WebullError
        except Exception as e:
            return {"available": False, "reason": f"webull SDK unavailable: {e}"}

        def _fetch() -> dict[str, Any]:
            # Webull() raises WebullError from its own constructor when
            # WEBULL_KEY/SECRET are missing (wb.credentials()), so there's no
            # separate `configured` check to make here -- the outer except
            # turns that into the {"available": False} shape.
            return Webull().portfolio()

        try:
            portfolio = await asyncio.to_thread(_fetch)
        except Exception as e:
            return {"available": False, "reason": str(e)}

        totals = portfolio.get("totals", {})
        return {
            "available": True,
            "stale": portfolio.get("stale", False),
            "fetch_error": portfolio.get("error"),
            "equity": totals.get("nlv"),
            "buying_power": totals.get("buying_power"),
            "option_buying_power": totals.get("option_buying_power"),
            "day_pl": totals.get("day_pl"),
            "unrealized_pl": totals.get("unrealized_pl"),
            "position_count": totals.get("position_count"),
            "positions": portfolio.get("positions", []),
            "fetched_at": portfolio.get("fetched_at"),
        }

    @mcp.tool()
    def get_halt_status() -> dict[str, Any]:
        """Whether Vesper's emergency freeze is currently engaged, and why.

        Read-only accessor only -- `core/halt.py`'s `halt()` / `resume()`
        are never imported here (see module docstring).
        """
        try:
            from core.halt import get_halt_status as _get_halt_status
        except Exception as e:
            return {"available": False, "reason": str(e)}
        try:
            return {"available": True, **_get_halt_status()}
        except Exception as e:
            return {"available": False, "reason": str(e)}

    @mcp.tool()
    def get_drawdown_status() -> dict[str, Any]:
        """Portfolio circuit breaker: the tracked peak NLV and the configured
        trailing-drawdown threshold that trips it (`VESPER_CIRCUIT_BREAKER_PCT`,
        default 15%). Report-only -- never calls
        `check_portfolio_drawdown()`, which can itself trigger a halt."""
        try:
            from core.circuit_breaker import get_peak_nlv, get_configured_threshold
        except Exception as e:
            return {"available": False, "reason": str(e)}
        try:
            return {
                "available": True,
                "peak_nlv": get_peak_nlv(),
                "configured_threshold_pct": get_configured_threshold(),
            }
        except Exception as e:
            return {"available": False, "reason": str(e)}

    @mcp.tool()
    def get_paper_positions() -> dict[str, Any]:
        """Currently open simulated (paper-trading) positions."""
        try:
            from core.paper_ledger import get_paper_positions as _get_paper_positions
        except Exception as e:
            return {"available": False, "reason": str(e)}
        try:
            positions = _get_paper_positions()
        except Exception as e:
            return {"available": False, "reason": str(e)}
        return {"available": True, "count": len(positions), "positions": positions}

    @mcp.tool()
    def get_paper_summary() -> dict[str, Any]:
        """Paper-trading account summary: NLV, realized/unrealized P&L, win
        rate, unswept premium/tax-reserve pools."""
        try:
            from core.paper_ledger import get_paper_summary as _get_paper_summary
        except Exception as e:
            return {"available": False, "reason": str(e)}
        try:
            return {"available": True, **_get_paper_summary()}
        except Exception as e:
            return {"available": False, "reason": str(e)}

    @mcp.tool()
    def list_alerts() -> dict[str, Any]:
        """Every armed/pending/triggered price alert, with each dynamic
        level (flip/pin/wall_above/wall_below) re-resolved live from TDPro.

        Reuses `vesper/alerts_runner.py`'s `_build_levels_of()` -- the same
        `levels_of()` the live watcher thread runs on -- rather than
        re-deriving the TDPro-down behavior here, so rule 4c's guarantee
        stays defined in exactly one place: if TDPro can't answer, the
        level comes back as unavailable (`current_level: null,
        level_unavailable: true`), never a remembered number.
        """
        try:
            from alerts import AlertStore, resolve_level
            from vesper.alerts_runner import _build_levels_of
        except Exception as e:
            return {"available": False, "reason": str(e)}

        try:
            alerts = AlertStore().list()
        except Exception as e:
            return {"available": False, "reason": f"could not read alert store: {e}"}

        levels_of = _build_levels_of()
        level_cache: dict[str, Any] = {}
        out = []
        for a in alerts:
            sym = a["symbol"]
            dynamic = a.get("level_ref") is not None
            if dynamic:
                if sym not in level_cache:
                    try:
                        level_cache[sym] = levels_of(sym)
                    except Exception:
                        level_cache[sym] = None
                current_level = resolve_level(a, level_cache[sym])
            else:
                current_level = resolve_level(a, None)
            out.append({
                "id": a["id"],
                "symbol": sym,
                "direction": a["direction"],
                "level_ref": a["level_ref"],
                "level_static": a["level_static"],
                "current_level": current_level,
                "level_unavailable": dynamic and current_level is None,
                "state": a["state"],
                "note": a.get("note", ""),
                "last_price": a.get("last_price"),
                "repeat": a.get("repeat", False),
                "trigger_count": a.get("trigger_count", 0),
            })
        return {"available": True, "count": len(out), "alerts": out}

    @mcp.tool()
    def list_pending_proposals() -> dict[str, Any]:
        """Order proposals currently awaiting a human's Telegram/Discord tap.

        Read-only view of `ApprovalRegistry` -- never calls
        `submit_decision()` (see module docstring)."""
        try:
            from core.approval_registry import approval_registry
        except Exception as e:
            return {"available": False, "reason": str(e)}
        try:
            pending = approval_registry.list_pending()
        except Exception as e:
            return {"available": False, "reason": str(e)}
        return {"available": True, "count": len(pending), "proposals": pending}

    @mcp.tool()
    def get_proposal(proposal_id: str) -> dict[str, Any]:
        """One proposal's pending record and, if it has already been acted
        on, its recorded decision. Read-only -- never submits a decision."""
        try:
            from core.approval_registry import approval_registry
        except Exception as e:
            return {"available": False, "reason": str(e)}
        try:
            pending = approval_registry.get_pending(proposal_id)
            decision = approval_registry.get_decision(proposal_id)
        except Exception as e:
            return {"available": False, "reason": str(e)}
        return {
            "available": True,
            "found": pending is not None or decision is not None,
            "proposal_id": proposal_id,
            "pending": pending,
            "decision": decision,
        }

    @mcp.tool()
    def get_audit_trail(limit: int = 20) -> dict[str, Any]:
        """The most recent entries in the hash-chained audit ledger
        (`core/audit_chain.py`), newest last. `limit` <= 0 returns every
        entry. Does not verify the chain -- see `verify_audit_chain`."""
        try:
            from core import audit_chain
        except Exception as e:
            return {"available": False, "reason": str(e)}

        path = audit_chain._CHAIN_PATH
        if not path.exists():
            return {"available": True, "entry_count": 0, "returned": 0, "entries": []}

        try:
            with open(path, "rb") as f:
                lines = [line for line in f if line.strip()]
        except Exception as e:
            return {"available": False, "reason": str(e)}

        tail = lines[-limit:] if limit > 0 else lines
        entries = []
        for raw_line in tail:
            try:
                entries.append(json.loads(raw_line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # a corrupt line is verify_audit_chain's problem, not a crash here

        return {
            "available": True,
            "entry_count": len(lines),
            "returned": len(entries),
            "entries": entries,
        }

    @mcp.tool()
    def verify_audit_chain() -> dict[str, Any]:
        """Walk the audit chain and confirm every hash link, localizing the
        first break if the chain has been tampered with."""
        try:
            from core.audit_chain import verify_chain
        except Exception as e:
            return {"available": False, "reason": str(e)}
        try:
            return {"available": True, **verify_chain()}
        except Exception as e:
            return {"available": False, "reason": str(e)}

    @mcp.tool()
    def get_playbook_calibration(playbook: str, days: int = 90) -> dict[str, Any]:
        """Resolved win rate and calibration adjustment for one playbook
        over the trailing `days`, from the conviction journal
        (`mcp_server/conviction.py`). Read-only -- never appends a journal
        entry."""
        try:
            from core.conviction import get_playbook_performance
        except Exception as e:
            return {"available": False, "reason": f"conviction journal unavailable: {e}"}
        try:
            result = get_playbook_performance(playbook, days=days)
        except Exception as e:
            return {"available": False, "reason": str(e)}
        return {"available": True, **result}

    @mcp.tool()
    async def recall_similar_setups(
        query_thesis: str,
        top_k: int = 5,
        ticker: str | None = None,
        playbook: str | None = None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        """Semantic recall of similar historical setups and their outcomes
        from the trade-memory chroma collection (`mcp_server/knowledge.py`).

        `chromadb` is an optional, sometimes-heavy dependency (embeddings
        client, on-disk vector index) -- imported here lazily, inside the
        call, so a `chromadb`-less environment still gets a clean
        "unavailable" result instead of this whole module (and every other
        tool registered alongside it) failing to import.
        """
        try:
            from mcp_server.knowledge import recall_similar_setups as _recall
        except Exception as e:
            return {"available": False, "reason": f"trade memory unavailable: {e}"}
        try:
            results = await _recall(
                query_thesis=query_thesis,
                top_k=top_k,
                ticker=ticker,
                playbook=playbook,
                origin=origin,
            )
        except Exception as e:
            return {"available": False, "reason": f"trade memory query failed: {e}"}
        return {"available": True, "count": len(results), "results": results}

    @mcp.tool()
    async def get_position_monitor_status() -> dict[str, Any]:
        """What the exit cascade WOULD do to each open Webull position right
        now -- built stateless, fresh per call.

        `PositionMonitor.status()` only reflects a `vesper loop` process's
        in-memory instance, which this MCP server is not, so this instead
        constructs `core.position_preview.PositionPreviewMonitor` -- a
        guard-free read-only twin of `vesper.monitor.PositionMonitor` (see
        that module's docstring for why: importing `vesper.monitor` itself
        would pull `vesper.execution_guard`'s live `guard` singleton into
        `sys.modules` as an import side effect, which this read-only server
        must never do) -- polls live Webull positions
        (`poll_webull_positions()`) and runs the same deterministic
        `evaluate_position()` rules used by the real cascade -- reporting,
        not executing, whatever trigger comes back. It NEVER calls
        `execute_exit` or anything else that sells: this is read-only by
        construction, not by omission.

        Because a fresh `PositionPreviewMonitor` starts with no history, the
        peak-gain / breakeven-lock tracking `evaluate_position()` depends on
        for the trailing-stop rule always starts at 0.0 / unlocked here --
        this reports what the cascade would do on price/time/level rules
        alone, not the trailing-stop state a long-running `vesper loop`
        would have accumulated.
        """
        try:
            from core.position_preview import PositionPreviewMonitor
        except Exception as e:
            return {"available": False, "reason": str(e)}

        monitor = PositionPreviewMonitor()
        try:
            positions = await monitor.poll_webull_positions()
        except Exception as e:
            return {"available": False, "reason": f"could not poll positions: {e}"}

        out = []
        for pos in positions:
            try:
                trigger = monitor.evaluate_position(pos)
            except Exception as e:
                out.append({"symbol": pos.symbol, "error": str(e)})
                continue
            out.append({
                "symbol": pos.symbol,
                "asset_type": pos.asset_type,
                "quantity": pos.quantity,
                "entry_price": pos.entry_price,
                "current_price": pos.current_price,
                "unrealized_pnl_pct": pos.unrealized_pnl_pct,
                "would_exit": trigger is not None,
                "exit_reason": trigger.reason if trigger else None,
                "exit_urgency": trigger.urgency if trigger else None,
                "exit_sell_quantity": trigger.sell_quantity if trigger else None,
            })

        return {
            "available": True,
            "note": (
                "Stateless snapshot: trailing-stop peak-gain/breakeven-lock "
                "tracking resets every call, since this is a fresh "
                "PositionPreviewMonitor rather than vesper loop's long-running one."
            ),
            "position_count": len(out),
            "positions": out,
        }

    registered = [
        "get_account_state",
        "get_halt_status",
        "get_drawdown_status",
        "get_paper_positions",
        "get_paper_summary",
        "list_alerts",
        "list_pending_proposals",
        "get_proposal",
        "get_audit_trail",
        "verify_audit_chain",
        "get_playbook_calibration",
        "recall_similar_setups",
        "get_position_monitor_status",
    ]
    logger.info("Registered %d Vesper read-only tools", len(registered))
    return registered
