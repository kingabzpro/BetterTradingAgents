"""Offline check for the market-regime agent reuse cache: one regime read per
identical index snapshot (concurrent siblings join the in-flight call), a
failed run is retried, incognito bypasses reads and writes, and a changed
index snapshot misses.
Run: uv run python scripts/check_regime_cache.py
No network: index context, portfolio, hosted forecast, and the price snapshot
are stubbed; the agents run in mock mode with blank provider keys.
"""

import asyncio
import os
import random
import tempfile
from pathlib import Path

# Isolated DB + no provider keys, before app modules import.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "regime_cache_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["BAKO_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""

import app.workflow as workflow  # noqa: E402
from app.memory import init as memory_init  # noqa: E402
from app.tools.market_data import MarketData  # noqa: E402

asyncio.run(memory_init())  # live runs record decisions; give the temp DB its schema

_INDICES = {
    "spx": {"last": 5000.0, "change_5d_pct": 1.2, "change_21d_pct": 3.0, "above_sma20": True},
    "nasdaq": {"last": 16000.0, "change_5d_pct": 2.0, "change_21d_pct": 4.0, "above_sma20": True},
    "vix": {"last": 14.0, "change_5d_pct": -5.0},
    "rates_10y": {"last": 4.2, "change_5d_pct": 0.1},
}
_STRESS = {**_INDICES, "vix": {"last": 28.0, "change_5d_pct": 20.0}}


async def _fake_portfolio():
    return None


async def _fake_hosted_forecast(closes, horizon, ticker=""):  # noqa: ARG001
    return None


def _fake_context(indices):
    async def get():
        return indices

    return get


workflow.fetch_portfolio_summary = _fake_portfolio
workflow.get_hosted_forecast = _fake_hosted_forecast
workflow.get_market_context = _fake_context(_INDICES)

market_agent_calls = {"count": 0}
fail_next_market = {"armed": False}
_original_run_agent = workflow._run_agent


async def _counting_run_agent(mod, *args, **kwargs):
    if mod.NAME != "market":
        return await _original_run_agent(mod, *args, **kwargs)
    if fail_next_market["armed"]:
        fail_next_market["armed"] = False
        return None  # simulate a failed agent run: no events, nothing to cache
    market_agent_calls["count"] += 1
    return await _original_run_agent(mod, *args, **kwargs)


workflow._run_agent = _counting_run_agent
workflow._market_regime_cache.clear()

events: list[tuple[str, dict]] = []


async def _emit(kind, payload):
    events.append((kind, payload))


def _snapshot(ticker: str) -> MarketData:
    random.seed(7)
    closes = [100.0]
    for _ in range(129):
        closes.append(closes[-1] * (1 + random.uniform(-0.015, 0.02)))
    return MarketData(
        ticker=ticker,
        price=closes[-1],
        company_name="Test Corp",
        closes=closes,
        highs=[c * 1.01 for c in closes],
        lows=[c * 0.99 for c in closes],
        volumes=[1_000_000.0] * len(closes),
        fundamentals={},
        news=[],
        social=[],
        sources={"prices": "yfinance", "fundamentals": "none", "news": "none", "social": "none"},
        as_of="2026-10-20T20:00:00+00:00",
    )


async def _analyze(ticker: str, incognito: bool = False, indices=None):
    if indices is not None:
        workflow.get_market_context = _fake_context(indices)
    return await asyncio.wait_for(
        workflow.analyze_ticker(
            ticker,
            _emit,
            market_data=_snapshot(ticker),
            held_closes={},
            incognito=incognito,
            depth="pro",
        ),
        120,
    )


def _market_done(ticker: str) -> dict:
    hits = [
        p
        for kind, p in events
        if kind == "agent_completed" and p.get("agent") == "market" and p.get("ticker") == ticker
    ]
    assert hits, f"no market agent_completed event for {ticker}"
    return hits[0]


def _market_started(ticker: str) -> bool:
    return any(
        kind == "agent_started" and p.get("agent") == "market" and p.get("ticker") == ticker
        for kind, p in events
    )


def _market_event_index(ticker: str, kind: str) -> int:
    for i, (k, p) in enumerate(events):
        if k == kind and p.get("agent") == "market" and p.get("ticker") == ticker:
            return i
    raise AssertionError(f"no {kind} market event for {ticker}")


# Concurrent siblings of one run join the in-flight call: one agent run total.


async def _gather_two():
    return await asyncio.gather(_analyze("AAA"), _analyze("BBB"))


first, second = asyncio.run(_gather_two())
assert market_agent_calls["count"] == 1, market_agent_calls
done_a, done_b = _market_done("AAA"), _market_done("BBB")
assert {("cached" in done_a), ("cached" in done_b)} == {True, False}
assert first.market and second.market and first.market.signal == second.market.signal
# The joiner waited on the initiator's call: it reports that call's duration
# (the same seconds as the initiator), not a hidden 0.0.
joiner = done_a if "cached" in done_a else done_b
runner = done_b if "cached" in done_a else done_a
assert joiner["duration_s"] > 0, joiner
assert abs(joiner["duration_s"] - runner["duration_s"]) < 2, (joiner, runner)

# A later ticker with the same snapshot reuses the finished read, with the
# full started/completed event pair so the UI renders it like a real run.
asyncio.run(_analyze("CCC"))
assert market_agent_calls["count"] == 1, "later ticker must reuse the regime read"
assert _market_done("CCC").get("cached") is True
assert _market_started("CCC")
assert (
    _market_event_index("CCC", "agent_started")
    < _market_event_index("CCC", "agent_completed")
), "cached reads must announce running before completing"

# Incognito reads bypass the cache even when the snapshot is cached.
asyncio.run(_analyze("X1", incognito=True))
assert market_agent_calls["count"] == 2, "incognito must not read the cache"
assert "cached" not in _market_done("X1")

# Incognito writes never populate it: a later normal run with that snapshot
# still pays the agent call.
asyncio.run(_analyze("X2", incognito=True, indices=_STRESS))
assert market_agent_calls["count"] == 3
asyncio.run(_analyze("X3", indices=_STRESS))
assert market_agent_calls["count"] == 4, "incognito must not write the cache"
assert "cached" not in _market_done("X3")

# A failed regime run is not cached; the next ticker with the same snapshot
# retries it instead of joining a dead result.
_RELAXED = {**_INDICES, "vix": {"last": 12.0, "change_5d_pct": -8.0}}
fail_next_market["armed"] = True
failed = asyncio.run(_analyze("F1", indices=_RELAXED))
assert failed.market is None and market_agent_calls["count"] == 4
asyncio.run(_analyze("F2", indices=_RELAXED))
assert market_agent_calls["count"] == 5, "a failed run must not be cached"
assert _market_done("F2")

print(
    "regime cache OK: concurrent siblings share one call, failures retried, "
    "incognito isolated, content-keyed"
)
print("ALL CHECKS PASSED")
