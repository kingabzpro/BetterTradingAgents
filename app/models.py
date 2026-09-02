"""Pydantic models shared across the app."""

from typing import Literal

from pydantic import BaseModel, Field

Decision = Literal["BUY", "HOLD", "SELL"]
Signal = Literal["bullish", "bearish", "neutral", "positive", "negative", "unknown"]


class AgentResult(BaseModel):
    """Result of a single agent (technical / fundamental / news / bull / bear)."""

    agent: str
    signal: str = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""


class AnalystResult(BaseModel):
    """Output schema the three research analysts must return."""

    ticker: str
    signal: str = "neutral"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = ""


class DebateResult(BaseModel):
    """Output schema for the bull / bear agents."""

    score: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = ""


class ManagerResult(BaseModel):
    """Output schema for the portfolio manager."""

    ticker: str
    decision: Decision = "HOLD"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = ""
    bull_case: str = ""
    bear_case: str = ""


class StockAnalysis(BaseModel):
    """Everything we know about one ticker after a full run."""

    ticker: str
    company_name: str = ""
    price: float | None = None
    decision: Decision = "HOLD"
    confidence: float = 0.0
    summary: str = ""
    bull_case: str = ""
    bear_case: str = ""
    technical: AgentResult | None = None
    fundamental: AgentResult | None = None
    news: AgentResult | None = None
    bull: AgentResult | None = None
    bear: AgentResult | None = None
    duration_s: float = 0.0
    error: str | None = None
    suggested_size_usd: float | None = None
    risk_flags: list[str] = Field(default_factory=list)


class AnalysisRequest(BaseModel):
    tickers: list[str] = Field(min_length=1)

    def normalized(self) -> list[str]:
        seen: dict[str, None] = {}
        for raw in self.tickers:
            for part in raw.split(","):
                t = part.strip().upper()
                if t:
                    seen.setdefault(t, None)
        return list(seen.keys())


class AnalysisResponse(BaseModel):
    run_id: str
    tickers: list[str]


class RunStatus(BaseModel):
    run_id: str
    tickers: list[str]
    status: Literal["running", "completed", "failed"] = "running"
    mock_mode: bool = False
    started_at: float = 0.0
    duration_s: float = 0.0
    error: str | None = None
    results: dict[str, StockAnalysis] = Field(default_factory=dict)


class PortfolioPosition(BaseModel):
    id: int
    ticker: str
    quantity: float
    entry_price: float
    current_price: float | None = None
    cost: float = 0.0
    value: float | None = None
    pnl: float | None = None  # unrealized for open positions, realized for closed ones
    pnl_pct: float | None = None
    added_at: str = ""
    exit_price: float | None = None
    closed_at: str | None = None


class PortfolioSummary(BaseModel):
    starting_cash: float
    cash: float
    positions_value: float | None = None
    total_equity: float | None = None
    total_pnl: float | None = None
    realized_pnl: float = 0.0
    positions: list[PortfolioPosition] = []
    history: list[PortfolioPosition] = []


class PortfolioAddRequest(BaseModel):
    ticker: str
    quantity: float = Field(gt=0)
    entry_price: float | None = None


class PortfolioCloseRequest(BaseModel):
    position_id: int
    exit_price: float | None = Field(default=None, gt=0)
