"""Point-in-time market snapshots for walk-forward backtests (ROADMAP 2.2).

build_snapshot(ticker, as_of) reconstructs the MarketData the live pipeline
would have seen on that date: 6 months of OHLCV ending at as_of, company news
published on or before as_of (anti-look-ahead, enforced here), and
current-vintage fundamentals (a known, report-stated bias). Everything is
cached in SQLite keyed by ticker+date+window so warm re-runs make zero
network calls; BACKTEST_OFFLINE=1 turns cache misses into errors.
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


async def build_snapshot(
    ticker: str,
    as_of: str,
    cache: SnapshotCache,
    lookback_days: int = LOOKBACK_DAYS,
    news_days: int = NEWS_DAYS,
    offline: bool | None = None,
) -> MarketData:
    """MarketData as known at close of `as_of`, served from cache when warm."""
    offline = offline_mode() if offline is None else offline

    cached = cache.get_snapshot(ticker, as_of, lookback_days, news_days)
    if cached is not None:
        return market_from_payload(ticker, as_of, cached)

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

    fundamentals = cache.get_fundamentals(ticker)
    if fundamentals is None:
        if offline:
            fundamentals = {"company_name": ticker, "fundamentals": {}, "source": "none"}
        else:
            fundamentals = await fetch_fundamentals(ticker)
            cache.put_fundamentals(ticker, fundamentals)

    payload = {
        "history": history,
        "news": news,
        # Social posts are a current-vintage web search with no point-in-time
        # equivalent; replays see an empty set and the sentiment analyst
        # reports a thin-volume neutral instead of leaking future chatter.
        "social": [],
        "fundamentals": fundamentals.get("fundamentals", {}),
        "company_name": fundamentals.get("company_name", ticker),
        "sources": {
            "prices": "yfinance",
            "news": "finnhub" if news else "none",
            "social": "none",
            "fundamentals": fundamentals.get("source", "none"),
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
