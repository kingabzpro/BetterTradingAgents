"""Versioned option replay inputs and exchange scheduling."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
import re
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

CHICAGO = ZoneInfo("America/Chicago")
TICKERS = ("NVDA", "AMD", "META", "MSFT", "AAPL", "TSLA")
Mode = Literal["options_intraday_v1", "options_swing_v1"]
Preset = Literal["intraday_7_14_dte", "swing_5_sessions", "swing_10_sessions"]
Resolution = Literal["tick", "minute", "snapshot"]
QUOTE_LABELS = {"tick": "Historical quote backtest", "minute": "Historical minute-quote estimate"}
SYNTHETIC_LABEL = "Synthetic mechanics test"
REQUIRED_LABEL = "Historical options data required"
DAILY_INTRADAY_LABEL = "Archived daily-signal intraday options replay."
EXECUTION_POLICY_VERSION = "options-replay-v1-enforcement-1"
MAX_PREMIUM_BUDGET = Decimal("0.15")
SOURCE_TIME = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)")


def _nanoseconds(value: datetime) -> int:
    utc = value.astimezone(timezone.utc)
    seconds = (utc.date() - date(1970, 1, 1)).days * 86400 + utc.hour * 3600 + utc.minute * 60 + utc.second
    return seconds * 1_000_000_000 + utc.microsecond * 1000


def _source_nanoseconds(value: str) -> int:
    parts = SOURCE_TIME.fullmatch(value)
    if parts is None:
        raise ValueError("source_timestamp must be a timezone-aware RFC 3339 timestamp of up to nanosecond precision")
    seconds, fraction, offset = parts.groups()
    return _nanoseconds(datetime.fromisoformat(seconds + offset)) + int((fraction or "").ljust(9, "0"))


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Rules(Record):
    mode: Mode
    preset: Preset
    min_dte: int
    max_dte: int
    target_dte: int
    holding_sessions: int


PRESETS = {r.preset: r for r in (
    Rules(mode="options_intraday_v1", preset="intraday_7_14_dte", min_dte=7, max_dte=14, target_dte=10, holding_sessions=1),
    Rules(mode="options_swing_v1", preset="swing_5_sessions", min_dte=14, max_dte=21, target_dte=18, holding_sessions=5),
    Rules(mode="options_swing_v1", preset="swing_10_sessions", min_dte=28, max_dte=35, target_dte=32, holding_sessions=10),
)}


class Execution(Record):
    starting_cash: Decimal = Field(default=Decimal("2500.00"), gt=0, decimal_places=2, max_digits=10)
    premium_budget: Decimal = Field(default=MAX_PREMIUM_BUDGET, gt=0, le=MAX_PREMIUM_BUDGET, decimal_places=4)
    commission: Decimal = Field(default=Decimal("0.65"), ge=0, decimal_places=2, max_digits=6)
    other_fee_per_side: Decimal = Field(default=Decimal("0.00"), ge=0, decimal_places=2, max_digits=6)
    slippage: Decimal = Field(default=Decimal("0.01"), ge=0, decimal_places=4, max_digits=6)
    stress_slippage: Decimal = Field(default=Decimal("0.02"), ge=0, decimal_places=4, max_digits=6)

    @model_validator(mode="after")
    def validate_stress(self):
        if self.stress_slippage < self.slippage:
            raise ValueError("stress slippage must be at least primary slippage")
        return self


class Contract(Record):
    id: str = Field(min_length=1, max_length=80)
    ticker: str
    right: Literal["call", "put"]
    strike: Decimal = Field(gt=0)
    expiration: date
    listed_at: AwareDatetime
    last_trade_at: AwareDatetime
    multiplier: int = Field(gt=0)
    deliverable: Literal["standard_100_shares", "adjusted", "unknown"]

    @model_validator(mode="after")
    def check_dates(self):
        if self.listed_at >= self.last_trade_at or self.last_trade_at.astimezone(CHICAGO).date() > self.expiration:
            raise ValueError("contract listing and last trading time must precede expiration")
        return self


class Observation(Record):
    observed_at: AwareDatetime
    available_at: AwareDatetime
    source_timestamp: str | None = None
    source_sequence: int | None = Field(default=None, ge=0, strict=True)

    @property
    def chronological_key(self) -> tuple:
        original = _source_nanoseconds(self.source_timestamp) if self.source_timestamp else _nanoseconds(self.observed_at)
        return self.observed_at, original, self.source_sequence if self.source_sequence is not None else -1, self.available_at

    @field_validator("observed_at", "available_at", mode="before")
    @classmethod
    def check_precision(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("normalized timestamps must use ISO 8601 with a timezone offset")
        fraction = re.search(r"[.,](\d+)", value) if isinstance(value, str) else None
        if fraction and any(digit != "0" for digit in fraction[1][6:]):
            raise ValueError("round source timestamps up to a microsecond and retain the original source timestamp")
        return value

    @model_validator(mode="after")
    def check_causality(self):
        if self.available_at < self.observed_at:
            raise ValueError("an observation cannot be available before it is observed")
        if self.source_timestamp is not None:
            original = _source_nanoseconds(self.source_timestamp)
            if -(-original // 1000) * 1000 != _nanoseconds(self.observed_at):
                raise ValueError("observed_at must equal source_timestamp rounded up to a microsecond")
        return self


class Quote(Observation):
    contract_id: str
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(ge=0)
    bid_size: int | None = Field(default=None, ge=0)
    ask_size: int | None = Field(default=None, ge=0)
    bar_start: AwareDatetime | None = None

    @property
    def valid(self) -> bool:
        return 0 < self.bid <= self.ask


class Underlying(Observation):
    ticker: str
    price: Decimal = Field(gt=0)


class Adjustment(Record):
    contract_id: str
    effective_at: AwareDatetime
    evidence: str = Field(min_length=1)


class Signal(Record):
    day: date
    ticker: Literal["NVDA", "AMD", "META", "MSFT", "AAPL", "TSLA"]
    fast: Literal["BUY", "HOLD", "SELL", "FAILED"]
    momentum: Literal["BUY", "HOLD", "SELL"]
    input_cutoff: AwareDatetime
    available_at: AwareDatetime
    generated_at: AwareDatetime | None = None
    research_cost: Decimal = Field(default=Decimal("0.00"), ge=0, decimal_places=6)

    @model_validator(mode="after")
    def check_causality(self):
        if self.input_cutoff > self.available_at or (self.generated_at and not self.input_cutoff <= self.generated_at <= self.available_at):
            raise ValueError("signal cutoff, generation, and availability are out of order")
        return self


class SignalArchive(Record):
    source: Literal["archived_daily", "imported_timestamped", "synthetic"]
    candidate: str
    model: str
    provenance: dict
    signals: list[Signal]

    @model_validator(mode="after")
    def check_signals(self):
        seen = {(s.day, s.ticker) for s in self.signals}
        if len(seen) != len(self.signals):
            raise ValueError("duplicate ticker on a signal date")
        if self.source == "imported_timestamped" and (
            not {"prompts", "prompt_sha256", "settings", "input_source"} <= self.provenance.keys()
            or any(s.generated_at is None or "research_cost" not in s.model_fields_set for s in self.signals)
        ):
            raise ValueError("imported signals require generation times, prompts and hash, settings, input source, and explicit costs")
        return self


class Coverage(Record):
    discovery: bool
    entry_quotes: bool
    exit_quotes: bool
    underlying: bool
    adjustments: bool
    note: str = ""

    @property
    def ready(self) -> bool:
        return all((self.discovery, self.entry_quotes, self.exit_quotes, self.underlying, self.adjustments))


class Dataset(Record):
    schema_version: Literal[1]
    preset: Preset
    source: Literal["historical", "synthetic", "prospective"]
    resolution: Resolution
    provider: str = Field(min_length=1, max_length=160)
    entitlement: str = Field(min_length=1)
    covered_through: AwareDatetime
    coverage: dict[date, dict[str, Coverage]]
    contracts: list[Contract]
    underlying: list[Underlying]
    quotes: list[Quote]
    adjustments: list[Adjustment] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_tape(self):
        if (self.source == "prospective") != (self.resolution == "snapshot"):
            raise ValueError("prospective datasets use polled snapshot observations exclusively")
        ids = {c.id for c in self.contracts}
        if len(ids) != len(self.contracts):
            raise ValueError("duplicate option contract identifiers")
        if any(c.ticker not in TICKERS for c in self.contracts):
            raise ValueError("contract underlying is outside the study universe")
        if any(a.contract_id not in ids for a in self.adjustments):
            raise ValueError("adjustment refers to an unknown contract")
        seen = {}
        for q in self.quotes:
            if q.contract_id not in ids or q.available_at > self.covered_through:
                raise ValueError("option quote outside the declared data archive")
            key = (q.contract_id, q.chronological_key)
            previous = seen.setdefault(key, (q.bid, q.ask, q.bid_size, q.ask_size, q.bar_start))
            if previous != (q.bid, q.ask, q.bid_size, q.ask_size, q.bar_start):
                raise ValueError("ambiguous quote order: conflicting updates require an original timestamp or source sequence")
            if self.resolution == "minute":
                if q.bar_start is None or q.observed_at - q.bar_start != timedelta(minutes=1):
                    raise ValueError("minute quote bars require a start exactly one minute before observation")
            elif q.bar_start is not None:
                raise ValueError("tick and snapshot quotes cannot have a bar start")
        spots = {}
        for u in self.underlying:
            if u.available_at > self.covered_through or u.ticker not in TICKERS:
                raise ValueError("underlying observation outside the declared data archive")
            if spots.setdefault((u.ticker, u.chronological_key), u.price) != u.price:
                raise ValueError("ambiguous underlying order: conflicting observations require an original timestamp or source sequence")
        return self


@lru_cache(maxsize=1)
def calendar():
    import exchange_calendars

    return exchange_calendars.get_calendar("XNYS", start="1990-01-01", end="2050-12-31")


def schedule(day: date, available_at: datetime, rules: Rules) -> tuple[datetime, datetime]:
    cal = calendar()
    sessions = cal.sessions_in_range(day.isoformat(), (day + timedelta(days=60)).isoformat())
    for i, session in enumerate(sessions):
        entry = datetime.combine(session.date(), time(8, 40), CHICAGO)
        if entry > available_at and entry >= cal.session_open(session):
            exit_session = sessions[i + rules.holding_sessions - 1]
            return entry, cal.session_close(exit_session).to_pydatetime().astimezone(CHICAGO) - timedelta(minutes=15)
    raise ValueError("no exchange session available for this signal")


def session_bounds(when: datetime) -> tuple[datetime, datetime] | None:
    day = when.astimezone(CHICAGO).date().isoformat()
    cal = calendar()
    if not cal.is_session(day):
        return None
    return tuple(stamp.to_pydatetime().astimezone(CHICAGO) for stamp in (
        cal.session_open(day), cal.session_close(day),
    ))
