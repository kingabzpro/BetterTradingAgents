"""Offline check. Run: uv run python -m scripts.check_analysis_cache"""

import asyncio
import os
import tempfile
from pathlib import Path

# Isolated DB: _analyze_one reads "what changed since last call" through
# run_history, so the analysis_runs table must exist even on a clean machine
# (CI) where no developer portfolio.db is around.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "analysis_cache_test.db")

from app import run_history, runs  # noqa: E402
from app.models import PortfolioPosition, PortfolioSummary, StockAnalysis  # noqa: E402


async def main() -> None:
    await run_history.init()
    calls = 0
    original_analyze = runs.analyze_ticker

    async def fake_analyze(ticker: str, emit, **_kwargs) -> StockAnalysis:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return StockAnalysis(ticker=ticker, company_name="Test", price=100.0)

    runs.analyze_ticker = fake_analyze
    cache = runs.RunStore()
    portfolio = PortfolioSummary(cash=100_000)
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

    # P2.10: the portfolio part of the key ignores live price drift (values,
    # PnL, equity) but reacts to holdings, cash, and the high-water mark.
    flat = PortfolioSummary(cash=100_000)
    drifted = PortfolioSummary(
        cash=100_000, positions_value=12_345.67, total_equity=112_345.67, total_pnl=1_234.56
    )
    assert runs._portfolio_key(flat) == runs._portfolio_key(drifted)
    assert runs._portfolio_key(flat) != runs._portfolio_key(PortfolioSummary(cash=100_400))
    held = PortfolioSummary(
        cash=100_000,
        positions=[PortfolioPosition(id=1, ticker="NVDA", quantity=10.0, entry_price=100.0)],
    )
    held_drift = PortfolioSummary(
        cash=100_000,
        positions=[PortfolioPosition(id=1, ticker="NVDA", quantity=10.0, entry_price=100.0, current_price=999.0, value=9_990.0)],
    )
    assert runs._portfolio_key(held) == runs._portfolio_key(held_drift)
    assert runs._portfolio_key(held) != runs._portfolio_key(
        PortfolioSummary(
            cash=100_000,
            positions=[PortfolioPosition(id=1, ticker="NVDA", quantity=11.0, entry_price=100.0)],
        )
    )

    # P2.10: eviction caps the cache; the oldest entries fall out first.
    evictor = runs.RunStore()
    for index in range(runs.ANALYSIS_CACHE_MAX_ENTRIES + 5):
        evictor._cache_result((f"T{index}",), StockAnalysis(ticker=f"T{index}"))
    assert len(evictor.analysis_cache) == runs.ANALYSIS_CACHE_MAX_ENTRIES
    assert ("T0",) not in evictor.analysis_cache, "oldest entry must be evicted first"
    assert (
        f"T{runs.ANALYSIS_CACHE_MAX_ENTRIES + 4}",
    ) in evictor.analysis_cache, "newest entry must survive"

    print("analysis cache OK: ticker inputs cached independently of run ID")


asyncio.run(main())
