"""Point-in-time market snapshots for walk-forward backtests (ROADMAP 2.2).

build_snapshot(ticker, as_of) reconstructs the MarketData the live pipeline
would have seen on that date: 6 months of OHLCV ending at as_of and company
news published on or before as_of (anti-look-ahead, enforced here).
Fundamentals have no point-in-time source, so the honest default (ROADMAP
P1.2) excludes them from replays entirely; callers can opt in to
current-vintage fundamentals with fundamentals="current", which leaks later
information into historical runs and is flagged loudly in the report.
Everything is cached in SQLite keyed by ticker+date+window so warm re-runs
make zero network calls; BACKTEST_OFFLINE=1 turns cache misses into errors.
"""

import asyncio
import logging
from datetime import date, timedelta

from app.backtest.cache import SnapshotCache, offline_mode
from app.tools.market_data import (
    MarketData,
    _finnhub_fundamentals,
    get_news_between,
    get_ohlcv_between,
)

logger = logging.getLogger("backtest")

LOOKBACK_DAYS = 183  # ~6 months of history, same window the live pipeline uses
NEWS_DAYS = 14
FUNDAMENTALS_EXCLUDED = "excluded"
FUNDAMENTALS_CURRENT = "current"


def published_on_or_before(item: dict, as_of: str) -> bool:
    """Anti-look-ahead gate: keep only news whose publish date is known at T.

    Items without a parseable date are dropped - an unknown date could be a
    future one.
    """
    published = str(item.get("published") or "").strip()
    try:
        return date.fromisoformat(published[:10]) <= date.fromisoformat(as_of)
    except ValueError:
        return False


def _apply_fundamentals_mode(payload: dict, fundamentals: str) -> dict:
    """View of a (possibly cached) payload under the requested mode.

    Cached snapshots predate the exclusion default and may carry
    current-vintage fundamentals; the honest default strips them at read time
    so a warm cache can never leak what an honest rerun must not see.
    """
    if fundamentals != FUNDAMENTALS_EXCLUDED:
        return payload
    stripped = dict(payload)
    stripped["fundamentals"] = {}
    sources = dict(payload.get("sources", {}))
    sources["fundamentals"] = FUNDAMENTALS_EXCLUDED
    stripped["sources"] = sources
    return stripped


def market_from_payload(ticker: str, as_of: str, payload: dict) -> MarketData:
    history = payload["history"]
    return MarketData(
        ticker=ticker,
        price=history.get("price"),
        company_name=payload.get("company_name") or ticker,
        closes=history.get("closes", []),
        highs=history.get("highs", []),
        lows=history.get("lows", []),
        volumes=history.get("volumes", []),
        fundamentals=payload.get("fundamentals", {}),
        news=payload.get("news", []),
        social=payload.get("social", []),
        sources=payload.get("sources", {}),
        as_of=as_of,
    )


async def fetch_fundamentals(ticker: str) -> dict:
    """Current-vintage fundamentals + company name (one call per ticker)."""
    metrics = await _finnhub_fundamentals(ticker)
    return {
        "company_name": metrics.pop("company_name", "") or ticker,
        "fundamentals": metrics,
        "source": "finnhub" if metrics else "none",
    }


async def _current_fundamentals(
    ticker: str, cache: SnapshotCache, offline: bool
) -> dict:
    """Current-vintage fundamentals payload from the cache table or provider."""
    payload = cache.get_fundamentals(ticker)
    if payload is not None:
        return payload
    if offline:
        return {"company_name": ticker, "fundamentals": {}, "source": "none"}
    payload = await fetch_fundamentals(ticker)
    cache.put_fundamentals(ticker, payload)
    return payload


async def build_snapshot(
    ticker: str,
    as_of: str,
    cache: SnapshotCache,
    lookback_days: int = LOOKBACK_DAYS,
    news_days: int = NEWS_DAYS,
    offline: bool | None = None,
    fundamentals: str = FUNDAMENTALS_EXCLUDED,
) -> MarketData:
    """MarketData as known at close of `as_of`, served from cache when warm.

    `fundamentals` selects "excluded" (default: no point-in-time source
    exists, so replays run without the fundamental analyst) or "current"
    (current-vintage data - a stated look-ahead bias, opt-in only).
    """
    if fundamentals not in (FUNDAMENTALS_EXCLUDED, FUNDAMENTALS_CURRENT):
        raise ValueError(f"fundamentals mode must be excluded or current, got {fundamentals!r}")
    offline = offline_mode() if offline is None else offline

    cached = cache.get_snapshot(ticker, as_of, lookback_days, news_days)
    if cached is not None:
        if fundamentals == FUNDAMENTALS_CURRENT and cached.get("sources", {}).get(
            "fundamentals"
        ) in (FUNDAMENTALS_EXCLUDED, ""):
            # Snapshot was cached under the honest default; merge the
            # opted-in current-vintage fundamentals back in for this replay.
            merged = await _current_fundamentals(ticker, cache, offline)
            cached = dict(cached)
            cached["fundamentals"] = merged.get("fundamentals", {})
            cached["company_name"] = (
                merged.get("company_name") or cached.get("company_name") or ticker
            )
            sources = dict(cached.get("sources", {}))
            sources["fundamentals"] = merged.get("source", "none")
            cached["sources"] = sources
            cache.put_snapshot(ticker, as_of, lookback_days, news_days, cached)
        return market_from_payload(
            ticker, as_of, _apply_fundamentals_mode(cached, fundamentals)
        )

    if offline:
        raise RuntimeError(
            f"offline mode: no cached snapshot for {ticker} @ {as_of} "
            f"(lookback={lookback_days}d, news={news_days}d)"
        )

    decision_day = date.fromisoformat(as_of)
    history, news = await asyncio.gather(
        get_ohlcv_between(
            ticker,
            (decision_day - timedelta(days=lookback_days)).isoformat(),
            (decision_day + timedelta(days=1)).isoformat(),  # include as_of close
        ),
        get_news_between(
            ticker,
            (decision_day - timedelta(days=news_days)).isoformat(),
            as_of,
        ),
    )
    if not history.get("price"):
        raise ValueError(f"no price history for {ticker} up to {as_of}")
    # Anti-look-ahead: even a dated provider response only counts what was
    # published on or before the replayed date.
    news = [item for item in news if published_on_or_before(item, as_of)]

    if fundamentals == FUNDAMENTALS_CURRENT:
        fundamentals_payload = await _current_fundamentals(ticker, cache, offline)
        company_name = fundamentals_payload.get("company_name", ticker)
        fundamentals_data = fundamentals_payload.get("fundamentals", {})
        fundamentals_source = fundamentals_payload.get("source", "none")
    else:
        company_name = ticker
        fundamentals_data = {}
        fundamentals_source = FUNDAMENTALS_EXCLUDED

    payload = {
        "history": history,
        "news": news,
        # Social posts are a current-vintage web search with no point-in-time
        # equivalent; replays see an empty set and the sentiment analyst
        # reports a thin-volume neutral instead of leaking future chatter.
        "social": [],
        "fundamentals": fundamentals_data,
        "company_name": company_name,
        "sources": {
            "prices": "yfinance",
            "news": "finnhub" if news else "none",
            "social": "none",
            "fundamentals": fundamentals_source,
        },
    }
    cache.put_snapshot(ticker, as_of, lookback_days, news_days, payload)
    logger.info(
        "[backtest] %s @ %s: %d closes, %d news items (cached)",
        ticker,
        as_of,
        len(history.get("closes", [])),
        len(news),
    )
    return market_from_payload(ticker, as_of, payload)
