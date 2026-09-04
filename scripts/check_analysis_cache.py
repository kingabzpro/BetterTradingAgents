"""Offline check. Run: uv run python -m scripts.check_analysis_cache"""

import asyncio

from app import runs
from app.models import PortfolioSummary, StockAnalysis


async def main() -> None:
    calls = 0
    original_analyze = runs.analyze_ticker

    async def fake_analyze(ticker: str, emit, **_kwargs) -> StockAnalysis:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return StockAnalysis(ticker=ticker, company_name="Test", price=100.0)

    runs.analyze_ticker = fake_analyze
    cache = runs.RunStore()
    portfolio = PortfolioSummary(starting_cash=100_000, cash=100_000)
    first_run = runs.Run(["NVDA"], outlook="short_term", depth="medium")
    second_run = runs.Run(["NVDA"], outlook="short_term", depth="medium")
    try:
        first = await cache._analyze_one(first_run, "NVDA", portfolio)
        second = await cache._analyze_one(second_run, "NVDA", portfolio)
        assert calls == 1 and first is not second
        assert any(event.get("cached") for event in second_run.events)

        different_outlook = runs.Run(["NVDA"], outlook="long_term", depth="medium")
        await cache._analyze_one(different_outlook, "NVDA", portfolio)
        assert calls == 2, "inputs that change the answer must use another cache key"

        different_ticker = runs.Run(["AMD"], outlook="short_term", depth="medium")
        await cache._analyze_one(different_ticker, "AMD", portfolio)
        assert calls == 3, "each ticker must have its own cache entry"

        concurrent_cache = runs.RunStore()
        await asyncio.gather(
            concurrent_cache._analyze_one(
                runs.Run(["TSLA"], outlook="short_term", depth="medium"),
                "TSLA",
                portfolio,
            ),
            concurrent_cache._analyze_one(
                runs.Run(["TSLA"], outlook="short_term", depth="medium"),
                "TSLA",
                portfolio,
            ),
        )
        assert calls == 4 and not concurrent_cache.analysis_inflight
    finally:
        runs.analyze_ticker = original_analyze

    print("analysis cache OK: ticker inputs cached independently of run ID")


asyncio.run(main())
