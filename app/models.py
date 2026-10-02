"""Pydantic models shared across the app."""

from typing import Any, Literal
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


class JudgeResult(BaseModel):
    """Output schema for the debate judge (replaces the bull/bear rebuttals)."""

    agent: str = "judge"
    signal: str = "neutral"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    bull_strength: float = Field(default=0.5, ge=0.0, le=1.0)
    bear_strength: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = ""


class ManagerResult(BaseModel):
    """Output schema for the portfolio manager."""

    ticker: str
    decision: Decision = "HOLD"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = ""
    bull_case: str = ""
    bear_case: str = ""
    would_upgrade_if: str = ""
    would_downgrade_if: str = ""
    # Probability of the frozen success event (app/calibration.py SUCCESS_EVENT):
    # the call's direction beating SPY over the decision-memory horizon. It is
    # a separate, explicitly probabilistic field - `confidence` above stays
    # evidence strength and is never relabeled. HOLD carries no probability.
    probability_beat_spy: float | None = Field(default=None, ge=0.0, le=1.0)


class CalibrationTrackRecord(BaseModel):
    """Historical rates for one (decision, evidence bucket) scope, or an
    explicit unavailable state when the mature sample is too small (P1.1)."""

    decision: Decision = "HOLD"
    confidence_bucket: str = ""
    n_mature: int = 0
    min_observations: int = 0
    available: bool = False
    directional_hit_rate: float | None = None
    positive_alpha_rate: float | None = None
    mean_alpha_pct: float | None = None
    median_alpha_pct: float | None = None
    missed_upside_rate: float | None = None
    avoided_downside_rate: float | None = None
    mean_realized_pct: float | None = None
    models_pooled: int = 0


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


class DataQuality(BaseModel):
    """Server-computed trust metadata for one analysis (ROADMAP P0.1).

    Every field defaults so results persisted before this object existed still
    parse; the UI treats empty lists and a missing as_of as "not recorded".
    """

    as_of: str = ""
    age_hours: float | None = None  # age of the data at analysis time
    stale_after_hours: float | None = None  # outlook-specific staleness threshold
    stale: bool = False
    stale_reason: str = ""
    expected_analysts: list[str] = Field(default_factory=list)  # depth profile requested
    available_analysts: list[str] = Field(default_factory=list)  # returned usable output
    failed_analysts: list[str] = Field(default_factory=list)  # ran, produced nothing
    skipped_analysts: list[str] = Field(default_factory=list)  # excluded at this depth
    provider_fallbacks: list[str] = Field(default_factory=list)  # degraded inputs, plain text


class ConcentrationCheck(BaseModel):
    """Deterministic correlated-exposure warning for a sized BUY (ROADMAP P1.3).

    Warning-only: it never changes the decision. status: ok = within the
    correlated-group cap; high = past it (before/after equity shares recorded);
    unknown = a correlation could not be computed, which is reported rather
    than silently read as independent. None on the analysis = not checked
    (no surviving BUY, no portfolio, or a pre-P1.3 run).
    """

    status: Literal["ok", "high", "unknown"] = "unknown"
    detail: str = ""
    correlated: list[str] = Field(default_factory=list)  # holdings above the threshold
    unverified: list[str] = Field(default_factory=list)  # holdings with no usable history
    before_pct: float | None = None  # correlated-group share of equity before the BUY
    after_pct: float | None = None  # group share after the suggested size is added
    cap_pct: float | None = None  # the configured group cap, for display


class PreviousCall(BaseModel):
    """Snapshot of the earlier completed call a repeat analysis is compared
    against for the deterministic `What changed` summary (ROADMAP P1.5)."""

    run_id: str = ""
    analyzed_at: float | None = None
    decision: Decision | None = None
    confidence: float | None = None  # evidence strength, never a profit probability
    signals: dict[str, str] = Field(default_factory=dict)  # analyst key -> signal
    forecast_price_5d: float | None = None
    as_of: str = ""
    stale: bool = False
    risk_flags: list[str] = Field(default_factory=list)


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
    manager_decision: Decision | None = None  # the manager's call before the risk gate
    manager_confidence: float | None = Field(
        default=None, ge=0.0, le=1.0
    )  # evidence strength before the risk gate; None = not recorded (old runs)
    manager_probability: float | None = Field(
        default=None, ge=0.0, le=1.0
    )  # probability of the frozen success event; None = not asked or HOLD
    summary: str = ""
    bull_case: str = ""
    bear_case: str = ""
    would_upgrade_if: str = ""
    would_downgrade_if: str = ""
    technical: AgentResult | None = None
    fundamental: AgentResult | None = None
    news: AgentResult | None = None
    sentiment: AgentResult | None = None
    forecast: AgentResult | None = None
    bull: AgentResult | None = None
    bear: AgentResult | None = None
    bull_rebuttal: AgentResult | None = None  # deprecated: runs before the judge
    bear_rebuttal: AgentResult | None = None  # deprecated: runs before the judge
    judge: JudgeResult | None = None
    duration_s: float = 0.0
    error: str | None = None
    suggested_size_usd: float | None = None
    risk_flags: list[str] = Field(default_factory=list)
    concentration: ConcentrationCheck | None = None  # correlated-exposure warning (P1.3)
    past_decisions: list[dict] = Field(default_factory=list)  # graded prior calls on this ticker
    previous: PreviousCall | None = None  # earlier completed call on this ticker (P1.5)
    what_changed: list[str] = Field(default_factory=list)  # deterministic diff vs previous
    token_usage: dict = Field(default_factory=dict)  # summed LLM tokens for this ticker's run
    cost_estimate: dict = Field(default_factory=dict)  # app/cost.py estimate; {} = no tokens recorded
    as_of: str = ""
    providers: dict[str, str] = Field(default_factory=dict)
    source_references: list[SourceReference] = Field(default_factory=list)
    data_quality: DataQuality = Field(default_factory=DataQuality)


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
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
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
    status: Literal["running", "completed", "failed", "cancelled"]
    mock_mode: bool = False
    started_at: float
    duration_s: float = 0.0
    error: str | None = None
    result_count: int = 0
    has_errors: bool = False
    decisions: dict[str, Decision] = Field(default_factory=dict)
    cost_usd: float | None = None  # summed cost estimate; None = no tokens recorded (mock mode)
    cost_unknown: bool = False  # tokens exist but a model had no known list price


class ClearHistoryResponse(BaseModel):
    deleted: int


class CancelRunResponse(BaseModel):
    """Result of POST /api/runs/{run_id}/cancel (ROADMAP P0.2)."""

    run_id: str
    status: Literal["running", "completed", "failed", "cancelled"]


class ChatMessage(BaseModel):
    """One turn of the follow-up chat with the portfolio manager."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class ManagerChatRequest(BaseModel):
    """A follow-up question about one ticker of a finished run."""

    ticker: str
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)


class ManagerChatResponse(BaseModel):
    ticker: str
    answer: str
    mock_mode: bool = False


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
    external: bool = False


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


class WatchlistCall(BaseModel):
    """One recorded analysis used as a watchlist baseline or current call (P1.4)."""

    run_id: str = ""
    analyzed_at: float | None = None
    decision: Decision | None = None
    confidence: float | None = None  # evidence strength, never a profit probability
    price: float | None = None
    as_of: str = ""
    risk_flags: list[str] = Field(default_factory=list)


class WatchlistItem(BaseModel):
    """A saved ticker with its baseline call and any newer reanalysis (P1.4)."""

    id: int
    ticker: str
    note: str = ""
    outlook: Outlook = DEFAULT_OUTLOOK
    depth: Depth = DEFAULT_DEPTH
    added_at: str = ""
    last_call: WatchlistCall | None = None  # baseline: the call being tracked
    current_call: WatchlistCall | None = None  # newest comparable completed call, if any
    change_status: Literal["not_reanalyzed", "no_change", "changed"] = "not_reanalyzed"
    decision_changed: bool = False
    evidence_changed: bool = False  # Low/Moderate/Strong bucket moved
    price_move_pct: float | None = None  # baseline price -> live price
    live_price: float | None = None
    data_age_hours: float | None = None  # age of current_call.as_of when present
    unresolved_risk_flags: list[str] = Field(default_factory=list)
    view_run_id: str | None = None  # prefer current call's run, else baseline


class WatchlistAddRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=12)
    note: str = Field(default="", max_length=500)
    outlook: Outlook = DEFAULT_OUTLOOK
    depth: Depth = DEFAULT_DEPTH
    run_id: str | None = Field(
        default=None, max_length=64
    )  # snapshot the baseline from this run's result


class WatchlistUpdateRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    outlook: Outlook | None = None
    depth: Depth | None = None


class WatchlistAddResponse(BaseModel):
    item: WatchlistItem
    already_watched: bool = False


class BrokerStatus(BaseModel):
    """Whether the Alpaca paper connection exists and submissions are allowed.

    Never carries credentials. enabled=False means the kill switch
    (ALPACA_TRADING_ENABLED) is off: reads still work, orders do not.
    """

    configured: bool = False
    enabled: bool = False
    paper_url: str = "https://docs.alpaca.markets/us/docs/paper-trading"
    max_order_usd: float = 0.0  # 0 = no cap recorded (unconfigured)


class BrokerAccount(BaseModel):
    """Mapped Alpaca paper account snapshot."""

    account_number: str = ""
    status: str = ""
    equity: float | None = None
    cash: float | None = None
    buying_power: float | None = None
    last_equity: float | None = None
    trading_blocked: bool = False


class BrokerPosition(BaseModel):
    """Mapped open Alpaca paper position."""

    symbol: str
    quantity: float
    avg_entry_price: float
    current_price: float | None = None
    market_value: float | None = None
    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None


class BrokerOrder(BaseModel):
    """One order this app placed, from the local broker_orders row."""

    client_order_id: str
    alpaca_order_id: str | None = None
    run_id: str
    ticker: str
    side: Literal["buy", "sell"]
    notional: float
    status: str  # pending_submit | unknown | an Alpaca order status
    decision: str | None = None
    confidence: float | None = None
    decision_as_of: str | None = None
    filled_qty: float | None = None
    filled_avg_price: float | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


class BrokerOrderRequest(BaseModel):
    """Body of POST /api/broker/orders; confirm must be explicitly true."""

    run_id: str = Field(min_length=1, max_length=64)
    ticker: str = Field(min_length=1, max_length=12)
    side: Literal["buy", "sell"]
    notional: float = Field(gt=0, le=1_000_000)
    confirm: bool = False


class AutomationOrderOutcome(BaseModel):
    """What one automated order attempt did (submitted, or why it was not)."""

    ticker: str
    side: Literal["buy", "sell"]
    notional: float | None = None
    status: str = "skipped"  # an Alpaca order status, or skipped
    client_order_id: str | None = None
    error: str | None = None


class AutomationSession(BaseModel):
    """One automated trading session: one scan -> one agent run -> orders."""

    id: int = 0
    trigger: Literal["schedule", "manual"] = "schedule"
    status: Literal["running", "completed", "failed", "skipped"] = "running"
    started_at: float = 0.0  # epoch seconds
    finished_at: float | None = None
    run_id: str | None = None  # links to the analysis run and every order
    tickers: list[str] = Field(default_factory=list)
    decisions: dict[str, str] = Field(default_factory=dict)
    orders: list[AutomationOrderOutcome] = Field(default_factory=list)
    error: str | None = None


class AutomationStatus(BaseModel):
    """Current autopilot state for the portfolio page card."""

    enabled: bool = False
    session_active: bool = False
    configured: bool = False  # Alpaca keys present
    trading_enabled: bool = False  # submissions kill switch off = False
    llm_configured: bool = False
    interval_minutes: int = 0
    candidates: int = 0
    min_confidence: float = 0.0
    outlook: str = "short_term"
    depth: str = "medium"
    allow_sells: bool = True
    next_check_at: float | None = None
    sessions: list[AutomationSession] = Field(default_factory=list)


class AutomationUpdateRequest(BaseModel):
    """Body of POST /api/automation; flips the persisted on/off toggle."""

    enabled: bool


class SettingsUpdateRequest(BaseModel):
    """Body of POST /api/settings. values maps setting name to its new value
    (blank or None for a secret means unchanged); clear_secrets lists secret
    names whose keychain/DB entries should be deleted."""

    values: dict[str, Any] = Field(default_factory=dict)
    clear_secrets: list[str] = Field(default_factory=list)

