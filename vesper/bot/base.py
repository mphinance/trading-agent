"""Abstract Approval Channel Interface (Channel-Agnostic Bot Engine)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from vesper.state import OrderProposal, ExecutionResult


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

    @classmethod
    def from_proposal(cls, prop: OrderProposal, thesis: str = "") -> ProposalCard:
        cost = prop.estimated_cost or (prop.limit_price * prop.quantity * (100.0 if prop.asset_type == "OPTION" else 1.0))
        return cls(
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
            thesis=thesis,
        )

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
        if self.thesis:
            lines.append(f"**Thesis**: {self.thesis}")
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
