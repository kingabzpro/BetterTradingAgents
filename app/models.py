"""Pydantic models shared across the app."""

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.depth import DEFAULT_DEPTH, Depth
from app.outlook import DEFAULT_OUTLOOK, Outlook

Decision = Literal["BUY", "HOLD", "SELL"]
Signal = Literal["bullish", "bearish", "neutral", "positive", "negative", "unknown"]


class AgentResult(BaseModel):
    """Result of a single agent (technical / fundamental / news / sentiment / forecast / bull / bear)."""

    agent: str
    signal: str = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""


class AnalystResult(BaseModel):
    """Output schema every research analyst must return."""

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


class SourceReference(BaseModel):
    """A safe, display-ready reference to evidence used by the analysis."""

    kind: Literal["price", "fundamentals", "news", "social"]
    title: str
    provider: str
    url: str = ""
    published_at: str | None = None

    @field_validator("url", mode="before")
    @classmethod
    def allow_only_web_urls(cls, value: object) -> str:
        """Never send script, data, or local URLs to the browser."""
        if not isinstance(value, str):
            return ""
        candidate = value.strip()
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return ""
        return (
            candidate
            if parsed.scheme in {"http", "https"} and parsed.netloc
            else ""
        )


class StockAnalysis(BaseModel):
    """Everything we know about one ticker after a full run."""

    ticker: str
    company_name: str = ""
    price: float | None = None
    forecast_price_5d: float | None = None
    forecast_change_5d_pct: float | None = None
    forecast_trend_r2: float | None = None
    forecast_method: str = ""
    forecast_band_pct: float | None = None  # +/-1 sigma 5-day noise band, in %
    forecast_z: float | None = None  # forecast change / noise band
    decision: Decision = "HOLD"
    confidence: float = 0.0
    summary: str = ""
    bull_case: str = ""
    bear_case: str = ""
    technical: AgentResult | None = None
    fundamental: AgentResult | None = None
    news: AgentResult | None = None
    sentiment: AgentResult | None = None
    forecast: AgentResult | None = None
    bull: AgentResult | None = None
    bear: AgentResult | None = None
    bull_rebuttal: AgentResult | None = None
    bear_rebuttal: AgentResult | None = None
    duration_s: float = 0.0
    error: str | None = None
    suggested_size_usd: float | None = None
    risk_flags: list[str] = Field(default_factory=list)
    past_decisions: list[dict] = Field(default_factory=list)  # graded prior calls on this ticker
    token_usage: dict = Field(default_factory=dict)  # summed LLM tokens for this ticker's run
    as_of: str = ""
    providers: dict[str, str] = Field(default_factory=dict)
    source_references: list[SourceReference] = Field(default_factory=list)


class AnalysisRequest(BaseModel):
    tickers: list[str] = Field(min_length=1)
    outlook: Outlook = DEFAULT_OUTLOOK
    depth: Depth = DEFAULT_DEPTH
    client_id: str | None = Field(
        default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )

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
    outlook: Outlook = DEFAULT_OUTLOOK
    depth: Depth = DEFAULT_DEPTH
    status: Literal["running", "completed", "failed"] = "running"
    mock_mode: bool = False
    started_at: float = 0.0
    duration_s: float = 0.0
    error: str | None = None
    results: dict[str, StockAnalysis] = Field(default_factory=dict)


class RunHistoryItem(BaseModel):
    """Compact metadata for the analysis-history list."""

    run_id: str
    tickers: list[str]
    outlook: Outlook = DEFAULT_OUTLOOK
    depth: Depth = DEFAULT_DEPTH
    status: Literal["running", "completed", "failed"]
    mock_mode: bool = False
    started_at: float
    duration_s: float = 0.0
    error: str | None = None
    result_count: int = 0
    has_errors: bool = False
    decisions: dict[str, Decision] = Field(default_factory=dict)


class ClearHistoryResponse(BaseModel):
    deleted: int


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
    external: bool = False  # tracked holding (manual entry / CSV import), not demo cash


class PortfolioSummary(BaseModel):
    starting_cash: float
    cash: float
    positions_value: float | None = None
    total_equity: float | None = None
    total_pnl: float | None = None
    realized_pnl: float = 0.0
    unpriced_count: int = 0  # open positions without a live price, excluded from totals
    positions: list[PortfolioPosition] = []
    history: list[PortfolioPosition] = []


class PortfolioAddRequest(BaseModel):
    ticker: str
    quantity: float = Field(gt=0)
    entry_price: float | None = None


class PortfolioCloseRequest(BaseModel):
    position_id: int
    exit_price: float | None = Field(default=None, gt=0)


class PortfolioImportItem(BaseModel):
    ticker: str
    quantity: float = Field(gt=0)
    entry_price: float | None = Field(default=None, gt=0)  # None = use the live price


class PortfolioImportRequest(BaseModel):
    positions: list[PortfolioImportItem] = Field(min_length=1, max_length=200)


class PortfolioImportResponse(BaseModel):
    imported: int
    errors: list[str] = []
