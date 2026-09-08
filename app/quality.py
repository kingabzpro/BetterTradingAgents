"""Deterministic data-quality metadata shown beside every call (ROADMAP P0.1).

Rules, not an LLM: which researchers were asked, which answered, how old the
data is, and whether any provider degraded. Staleness is defined per outlook -
the same 3-day-old snapshot is stale for a day trader and fresh for a long-term
investor - instead of one global threshold.
"""

from datetime import datetime, timezone

from app.config import settings
from app.models import AgentResult, DataQuality
from app.outlook import OUTLOOKS, normalize_outlook

# How old the fetched data may be before the analysis is flagged stale, by the
# horizon the user is actually trading. Anchored to each outlook's guidance:
# day-trade signals "decay within hours to days", a swing thesis holds for
# "days to a few weeks", long-term evidence is timing-tolerant.
STALE_AFTER_HOURS = {
    "day_trade": 24.0,
    "short_term": 168.0,  # 7 days
    "long_term": 1080.0,  # 45 days
}

ALL_RESEARCHERS = ("technical", "fundamental", "news", "sentiment", "forecast")


def stale_after_hours(outlook: object) -> float:
    return STALE_AFTER_HOURS[normalize_outlook(outlook)]


def age_hours(as_of: str, now: datetime | None = None) -> float | None:
    """Hours between the data timestamp and now; None when unparseable."""
    if not as_of:
        return None
    try:
        stamp = datetime.fromisoformat(as_of)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - stamp).total_seconds() / 3600)


def _fallback_notes(providers: dict[str, str], expected: list[str]) -> list[str]:
    """Plain-text notes for inputs that degraded below the configured provider."""
    notes: list[str] = []
    if "forecast" in expected:
        if providers.get("forecast") == "local" and settings.nixtla_api_key:
            notes.append("forecast: local model (TimeGPT unavailable)")
        elif providers.get("forecast") == "none":
            notes.append("forecast: not available")
    if "fundamental" in expected:
        fundamentals = providers.get("fundamentals")
        if fundamentals == "none":
            notes.append("fundamentals: not available")
        elif fundamentals == "yfinance" and settings.finnhub_api_key:
            notes.append("fundamentals: yfinance (finnhub returned nothing)")
    if "sentiment" in expected and providers.get("social") in ("none", ""):
        notes.append("social posts: none retrieved")
    return notes


def build(
    as_of: str,
    outlook: str,
    expected: tuple[str, ...] | list[str],
    results: dict[str, AgentResult | None],
    providers: dict[str, str],
    now: datetime | None = None,
) -> DataQuality:
    """Assemble the trust metadata for one finished analysis.

    `expected` is the depth profile's researcher list; `results` maps each
    expected researcher to its AgentResult or None (failed). Researchers not
    in `expected` were skipped at this depth, which is a choice, not a failure.
    """
    expected_list = [key for key in ALL_RESEARCHERS if key in expected]
    available = [key for key in expected_list if results.get(key) is not None]
    failed = [key for key in expected_list if results.get(key) is None]
    skipped = [key for key in ALL_RESEARCHERS if key not in expected]

    threshold = stale_after_hours(outlook)
    age = age_hours(as_of, now)
    stale = age is not None and age > threshold
    reason = ""
    if age is None:
        reason = "data timestamp unavailable"
    elif stale:
        label = OUTLOOKS[normalize_outlook(outlook)]["label"]
        reason = (
            f"data is {age:.0f}h old; the {label} outlook treats data older "
            f"than {threshold:.0f}h as stale"
        )
    return DataQuality(
        as_of=as_of,
        age_hours=None if age is None else round(age, 2),
        stale_after_hours=threshold,
        stale=stale,
        stale_reason=reason,
        expected_analysts=expected_list,
        available_analysts=available,
        failed_analysts=failed,
        skipped_analysts=skipped,
        provider_fallbacks=_fallback_notes(providers, expected_list),
    )
