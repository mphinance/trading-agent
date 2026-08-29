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
    sma_200: Optional[float] = None
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


class OrderLeg(BaseModel):
    """One leg of a multi-leg combo order. Only meaningful inside
    OrderProposal.legs -- see the note there for why this exists as a
    separate, explicit structure rather than a list of OrderProposal.

    asset_type defaults to OPTION (every multi-leg combo before Thega was
    options-only); option_type/strike/expiry are only meaningful when
    asset_type=="OPTION" and are None for an EQUITY leg (e.g. Thega's 100
    owned shares backing the covered call)."""
    side: str                       # BUY or SELL
    asset_type: str = "OPTION"      # OPTION or EQUITY
    option_type: Optional[str] = None    # call or put -- OPTION legs only
    strike: Optional[float] = None       # OPTION legs only
    expiry: Optional[str] = None         # OPTION legs only
    quantity: int = 1
    limit_price: float   # this leg's own premium (OPTION) or share price (EQUITY)
    contract_symbol: Optional[str] = None


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
    # Underlying-keyed swing-option stop (see vesper/monitor.py evaluate_position
    # step 5). None means "legacy/contract_pct-only" -- the flat stop_loss above
    # is still the only stop that ever applies. "underlying_level" means the
    # basis named in underlying_stop_basis is an ADDITIONAL independent trigger,
    # not a replacement. underlying_stop_basis is deliberately one of the exact
    # dict keys analyze_technicals() returns ("sma_200" | "ema_34" |
    # "keltner_lower") so no translation layer is ever needed between drafting
    # and monitoring.
    underlying_stop_type: Optional[str] = None
    underlying_stop_basis: Optional[str] = None
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
    # Multi-leg orders (synthetic long, spreads, etc). When legs is set, this
    # is a combo order: strategy_type names a formula execution_guard knows
    # how to risk-assess (see execution_guard._MULTI_LEG_RISK_FORMULAS), and
    # the top-level side/limit_price/strike/option_type above describe the
    # net position for display only -- they are not sent to the broker.
    # estimated_cost/max_risk are still the authoritative net figures used
    # for approval-card display and audit trail; execution_guard recomputes
    # its own from the legs rather than trusting these at guard time.
    strategy_type: Optional[str] = None
    legs: Optional[List[OrderLeg]] = None

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
