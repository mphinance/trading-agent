"""Trading State definitions for LangGraph Quant Agent."""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional, Union
from typing_extensions import TypedDict
from pydantic import BaseModel, Field
import operator


class Candidate(BaseModel):
    """Screened stock candidate."""
    ticker: str
    source: str = Field(..., description="VCP, SQUEEZE, 0DTE_FLOW, WHALE_CONVERGENCE, USER_SPECIFIED")
    score: float = 0.0
    rationale: str = ""
    catalyst: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class MarketRegime(BaseModel):
    """Market Posture & Gamma Structure."""
    posture: str = "NEUTRAL"  # BULLISH, BEARISH, RANGEBOUND, HIGH_RISK_DISTRIBUTION
    health_score: float = 0.0
    health_label: str = "UNKNOWN"
    spy_spot: Optional[float] = None
    spy_gex_regime: Optional[str] = None
    spy_gamma_flip: Optional[float] = None
    macro_regime: Optional[str] = None
    distribution_days: int = 0
    notes: List[str] = Field(default_factory=list)


class TechnicalAudit(BaseModel):
    """Technical indicator breakdown."""
    ticker: str
    close: float
    rsi_14: float
    rsi_state: str
    macd_signal: Union[str, float] = "NEUTRAL"
    ema_stack: str = "NEUTRAL"  # BULLISH, BEARISH, COMPRESSED
    ema_8: Optional[float] = None
    ema_21: Optional[float] = None
    ema_34: Optional[float] = None
    ema_55: Optional[float] = None
    ema_89: Optional[float] = None
    atr_14: Optional[float] = None
    adx_14: Optional[float] = None
    rsi_2: Optional[float] = None
    rsi_2_prev: Optional[float] = None
    slow_k: Optional[float] = None
    slow_d: Optional[float] = None
    keltner_lower: Optional[float] = None
    keltner_basis: Optional[float] = None
    keltner_upper: Optional[float] = None
    summary: str = ""


class OptionAudit(BaseModel):
    """Options contract pricing & VoPR™ grade."""
    ticker: str
    option_type: str = "call"  # call or put
    strike: float
    expiry: str
    dte: int
    delta: float = 0.0
    theta: float = 0.0
    iv: float = 0.0
    vopr_grade: str = "N/A"
    return_on_capital: Optional[float] = None
    recommendation: str = ""


class OrderProposal(BaseModel):
    """Drafted order awaiting risk validation & human approval."""
    id: str
    ticker: str
    asset_type: str = "EQUITY"  # EQUITY or OPTION
    side: str = "BUY"           # BUY or SELL
    order_type: str = "LIMIT"   # LIMIT or MARKET
    quantity: int = 1
    limit_price: float
    stop_loss: Optional[float] = None
    profit_target: Optional[float] = None
    contract_symbol: Optional[str] = None
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    estimated_cost: float = 0.0
    max_risk: float = 0.0
    risk_reward_ratio: float = 0.0
    account_id: Optional[str] = None
    approved: bool = False
    rejection_reason: Optional[str] = None

    @property
    def target_price(self) -> Optional[float]:
        return self.profit_target

    @property
    def max_risk_usd(self) -> float:
        return self.max_risk


class ExecutionResult(BaseModel):
    """Execution status and fill confirmation."""
    order_proposal_id: str
    ticker: str
    status: str = "SUBMITTED"  # FILLED, SUBMITTED, REJECTED, DRY_RUN_SIMULATED, FAILED
    client_order_id: Optional[str] = None
    webull_order_id: Optional[str] = None
    filled_quantity: int = 0
    filled_price: float = 0.0
    fees: float = 0.0
    message: str = ""
    timestamp: Optional[str] = None


class TradingState(TypedDict):
    """Complete LangGraph Trading Agent State."""
    session_id: str
    mode: str  # 'autonomous', 'manual', 'dry_run'
    selected_playbook: str  # 'momentum_squeeze', '0dte_flow', 'institutional_convergence', 'all'
    target_ticker: Optional[str]
    
    # Analysis & State
    regime: Optional[MarketRegime]
    candidates: Annotated[List[Candidate], operator.add]
    technicals: Annotated[Dict[str, TechnicalAudit], operator.ior]
    options_audits: Annotated[Dict[str, OptionAudit], operator.ior]
    
    # Orders & Risk
    proposals: List[OrderProposal]
    rejected_proposals: Annotated[List[OrderProposal], operator.add]
    execution_results: List[ExecutionResult]
    
    # Human-in-the-Loop & Audit
    needs_human_approval: bool
    human_decision: Optional[str]  # 'APPROVE', 'REJECT', 'ABORT'
    persona: Optional[str]  # 'default', 'traderlady'
    audit_trail: Annotated[List[Dict[str, Any]], operator.add]
    reflection_notes: Annotated[List[str], operator.add]
    errors: Annotated[List[str], operator.add]
