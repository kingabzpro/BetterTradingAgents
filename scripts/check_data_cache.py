"""Offline checks for the live data TTL cache (release batch 2, item 9).

Covers the cache helper semantics and the get_stock_data wiring: a second
analysis of the same ticker inside the TTL reuses the news and social
responses instead of re-paying every provider; TTL=0 disables reuse; empty
responses are never cached. No network.

Run: uv run python -m scripts.check_data_cache
"""

import asyncio
import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "data_cache_test.db")

from app.config import settings  # noqa: E402
from app.tools import market_data  # noqa: E402

calls = {"news": 0, "social": 0, "web": 0, "yf_all": 0}


def _yf_payload(ticker: str) -> dict:
    return {
        "closes": [100.0 + i for i in range(30)],
        "highs": [101.0] * 30,
        "lows": [99.0] * 30,
        "volumes": [1_000_000] * 30,
        "price": 129.0,
        "name": f"{ticker} Inc",
        "fundamentals": {},
        "news": [],
    }


async def fake_yf_all(ticker: str) -> dict:
    calls["yf_all"] += 1
    return _yf_payload(ticker)


async def fake_finnhub_fundamentals(ticker: str) -> dict:
    return {}


async def fake_finnhub_news(ticker: str, *args, **kwargs) -> list[dict]:
    calls["news"] += 1
    return [{"title": "t", "summary": "s", "published": "", "url": "", "source": "x"}]


async def fake_social(ticker: str) -> list[dict]:
    calls["social"] += 1
    return [{"title": "post", "summary": "s", "published": "", "url": "", "source": "y"}]


async def checks() -> None:
    # ---- helper semantics -----------------------------------------------------
    market_data._response_cache.clear()
    counted = {"n": 0}

    async def fetch():
        counted["n"] += 1
        return ["value"]

    assert await market_data._ttl_cached("k", fetch, 900) == ["value"]
    assert await market_data._ttl_cached("k", fetch, 900) == ["value"]
    assert counted["n"] == 1, counted  # second call served from the cache
    await market_data._ttl_cached("other", fetch, 900)
    assert counted["n"] == 2, counted  # keys never mix
    await market_data._ttl_cached("k", fetch, 0)
    assert counted["n"] == 3, counted  # ttl=0 bypasses the cache

    empty = {"n": 0}

    async def fetch_empty():
        empty["n"] += 1
        return []

    await market_data._ttl_cached("e", fetch_empty, 900)
    await market_data._ttl_cached("e", fetch_empty, 900)
    assert empty["n"] == 2, empty  # failures/empties are never cached
    print("ttl cache semantics OK")

    # ---- get_stock_data wiring: news/social fetched once inside the TTL -------
    real = (
        market_data._yf_all,
        market_data._finnhub_fundamentals,
        market_data._finnhub_news,
        market_data._olostep_social,
    )
    market_data._yf_all = fake_yf_all
    market_data._finnhub_fundamentals = fake_finnhub_fundamentals
    market_data._finnhub_news = fake_finnhub_news
    market_data._olostep_social = fake_social
    market_data._response_cache.clear()
    settings.news_cache_ttl_seconds = 900
    try:
        first = await market_data.get_stock_data("TEST")
        second = await market_data.get_stock_data("TEST")
        assert calls["news"] == 1, calls  # one provider call for two analyses
        assert calls["social"] == 1, calls
        assert calls["yf_all"] == 2, calls  # prices stay fresh: never cached
        assert first.news == second.news and first.social == second.social

        settings.news_cache_ttl_seconds = 0
        await market_data.get_stock_data("TEST")
        assert calls["news"] == 2 and calls["social"] == 2, calls  # off = fetch fresh
        settings.news_cache_ttl_seconds = 900
    finally:
        (
            market_data._yf_all,
            market_data._finnhub_fundamentals,
            market_data._finnhub_news,
            market_data._olostep_social,
        ) = real
    print("get_stock_data TTL wiring OK: news 1x, social 1x, prices always fresh")


asyncio.run(checks())
print("DATA CACHE CHECKS PASSED")
