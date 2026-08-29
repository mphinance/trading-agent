"""Abstract Approval Channel Interface (Channel-Agnostic Bot Engine)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from vesper.state import OrderProposal, ExecutionResult, TradingState


@dataclass
class ProposalCard:
    proposal_id: str
    ticker: str
    side: str
    quantity: int
    asset_type: str
    limit_price: float
    est_cost: float
    max_risk_usd: float
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    thesis: str = ""
    time_stop: str = "15:00 ET"
    # Which model (or "deterministic_fallback") produced `thesis` -- see
    # OrderProposal.thesis_source in vesper/state.py.
    thesis_source: Optional[str] = None
    # Worst-case notional (vesper/bot/card_builder.py) and its share of live
    # buying power. None when either can't be computed/fetched -- never a
    # fabricated 0.
    worst_case_notional: Optional[float] = None
    buying_power_impact_pct: Optional[float] = None
    # A digest of the drafted proposal (NOT the execution-time ticket -- see
    # card_builder.proposal_digest's docstring for why those differ) plus
    # the static TTL the eventual execution ticket will carry.
    proposal_digest: Optional[str] = None
    execution_ticket_ttl_sec: Optional[float] = None
    # Before/after allocation-bucket diff, from risk_gate_node's per-proposal
    # capital_snapshot (vesper/nodes/risk_gate.py). None when the snapshot
    # wasn't captured for this proposal (e.g. this card was built without a
    # `state` argument) rather than a placeholder count/notional.
    open_long_option_count_before: Optional[int] = None
    open_long_option_count_after: Optional[int] = None
    sector: Optional[str] = None
    sector_notional_before: Optional[float] = None
    sector_notional_after: Optional[float] = None

    @classmethod
    def from_proposal(
        cls,
        prop: OrderProposal,
        thesis: str = "",
        state: Optional[TradingState] = None,
    ) -> ProposalCard:
        """Build a card from a proposal, optionally enriched with the
        risk-gate-computed `state` (account_equity/live_buying_power/
        capital_snapshot -- see TradingState in vesper/state.py).

        `thesis` stays an explicit optional param for backward compatibility
        with existing call sites (and tests) that pass one directly; when
        omitted, falls back to `prop.thesis` (populated by playbooks.py's
        LLM enrichment step) rather than staying silently empty as before.
        """
        cost = prop.estimated_cost or (prop.limit_price * prop.quantity * (100.0 if prop.asset_type == "OPTION" else 1.0))
        card = cls(
            proposal_id=prop.id,
            ticker=prop.ticker,
            side=prop.side,
            quantity=prop.quantity,
            asset_type=prop.asset_type,
            limit_price=prop.limit_price,
            est_cost=cost,
            max_risk_usd=prop.max_risk or 0.0,
            target_price=prop.profit_target,
            stop_loss=prop.stop_loss,
            thesis=thesis or (prop.thesis or ""),
            thesis_source=prop.thesis_source,
        )

        # Worst-case notional + proposal digest need only the proposal
        # itself -- always computable (or cleanly None), no live state.
        from vesper.bot.card_builder import worst_case_notional as _notional_of, proposal_digest as _digest_of
        from vesper.execution_guard import TICKET_TTL_SEC

        card.worst_case_notional = _notional_of(prop)
        card.proposal_digest = _digest_of(prop)
        card.execution_ticket_ttl_sec = TICKET_TTL_SEC

        if state:
            live_bp = state.get("live_buying_power")
            if card.worst_case_notional is not None and live_bp:
                card.buying_power_impact_pct = card.worst_case_notional / live_bp

            snapshot = (state.get("capital_snapshot") or {}).get(prop.id)
            if snapshot:
                before_count = snapshot.get("open_long_option_count_before")
                if before_count is not None:
                    card.open_long_option_count_before = before_count
                    is_new_long_option = (
                        prop.asset_type == "OPTION" and prop.side.upper() in ("BUY", "LONG")
                    )
                    card.open_long_option_count_after = before_count + (1 if is_new_long_option else 0)

                card.sector = snapshot.get("sector")
                before_sector_notional = snapshot.get("sector_notional_before")
                if card.sector is not None and before_sector_notional is not None:
                    # Same one-line "added notional" formula risk_gate.py's
                    # own same-batch-stacking increment uses, so the "after"
                    # figure the card shows matches what risk_gate.py would
                    # actually stack against the next proposal in a batch.
                    added = prop.estimated_cost or (
                        prop.limit_price * prop.quantity * (100.0 if prop.asset_type == "OPTION" else 1.0)
                    )
                    card.sector_notional_before = before_sector_notional
                    card.sector_notional_after = before_sector_notional + added

        return card

    def format_text(self) -> str:
        asset_label = f"({self.asset_type})"
        lines = [
            f"⚡ **VESPER TRADE PROPOSAL**",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"**Ticker**: `{self.ticker}` {asset_label}",
            f"**Action**: **{self.side.upper()}** {self.quantity}x @ ${self.limit_price:.2f}",
            f"**Est. Cost**: ${self.est_cost:,.2f} | **Max Risk**: ${self.max_risk_usd:,.2f}",
        ]
        if self.stop_loss:
            lines.append(f"**Stop Loss**: ${self.stop_loss:.2f} | **Target**: ${self.target_price or 0.0:.2f}")
        if self.worst_case_notional is not None:
            line = f"**Worst-Case Notional**: ${self.worst_case_notional:,.2f}"
            if self.buying_power_impact_pct is not None:
                line += f" ({self.buying_power_impact_pct:.1%} of buying power)"
            lines.append(line)
        if self.open_long_option_count_before is not None:
            lines.append(
                f"**Open Long Options**: {self.open_long_option_count_before} → "
                f"{self.open_long_option_count_after}"
            )
        if self.sector and self.sector_notional_before is not None:
            lines.append(
                f"**{self.sector} Sector Notional**: ${self.sector_notional_before:,.2f} → "
                f"${self.sector_notional_after:,.2f}"
            )
        if self.thesis:
            thesis_line = f"**Thesis**: {self.thesis}"
            if self.thesis_source:
                thesis_line += f" _(via {self.thesis_source})_"
            lines.append(thesis_line)
        if self.proposal_digest:
            ttl_note = f", TTL {self.execution_ticket_ttl_sec:g}s" if self.execution_ticket_ttl_sec else ""
            lines.append(
                f"**Proposal Digest**: `{self.proposal_digest[:16]}…` "
                f"(execution ticket{ttl_note} will carry a different digest)"
            )
        lines.append(f"**Time Stop**: {self.time_stop}")
        return "\n".join(lines)


class ApprovalChannel(ABC):
    """Abstract base class for all notification and approval channels."""

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Name of the channel (e.g. 'telegram', 'discord', 'webhook')."""
        pass

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Returns True if the credentials for this channel are set."""
        pass

    @abstractmethod
    async def send_proposal_card(self, card: ProposalCard) -> Optional[str]:
        """Send an interactive proposal card with Approve/Reject buttons.
        
        Returns the external message/card ID if successful.
        """
        pass

    @abstractmethod
    async def send_execution_result(self, result: ExecutionResult) -> bool:
        """Send execution confirmation or rejection notice."""
        pass

    @abstractmethod
    async def send_alert(self, title: str, message: str, level: str = "INFO") -> bool:
        """Send general notification or market health alert."""
        pass
