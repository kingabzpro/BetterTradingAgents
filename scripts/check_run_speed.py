"""Offline checks for the ROADMAP P2.14 run-speed pass: the overlapped data
phase, provider hard timeouts, the market-context cache, and per-stage timings.
Run: uv run python scripts/check_run_speed.py
No network: provider calls are stubbed; the workflow leg replays an injected
snapshot in backtest mode with blank provider keys.
"""

import asyncio
import os
import random
import tempfile
from pathlib import Path

# Isolated DB + no provider keys, before app modules import.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "run_speed_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["BAKO_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""

from app.tools import market_data as market_data_mod  # noqa: E402

# ---- timeouts: caps and the wait_for helper ---------------------------------
assert market_data_mod.OLOSTEP_TIMEOUT == 20
assert market_data_mod.YF_HISTORY_TIMEOUT == 60
assert market_data_mod.YF_SLOW_TIMEOUT == 20


async def _slow():
    await asyncio.sleep(5)
    return "slow"


assert asyncio.run(market_data_mod._with_timeout(_slow(), 0.01, "fallback")) == "fallback"
print("timeouts OK: olostep 20s, yf history 60s, profile/news 20s, cap falls back")

# ---- market-context cache: once per run, not once per ticker ----------------
index_calls = {"count": 0}


def fake_index_stats(symbol):
    index_calls["count"] += 1
    return {"last": 100.0, "change_5d_pct": 0.5, "change_21d_pct": 1.0, "above_sma20": True}


original_index_stats = market_data_mod._index_stats
market_data_mod._index_stats = fake_index_stats
market_data_mod._market_context_cache = None
try:
    first = asyncio.run(market_data_mod.get_market_context())
    second = asyncio.run(market_data_mod.get_market_context())
    assert index_calls["count"] == 4, index_calls  # one per index, then cached
    assert first == second and "spx" in first
finally:
    market_data_mod._index_stats = original_index_stats
    market_data_mod._market_context_cache = None
print("market context OK: 4 index fetches served twice from the TTL cache")

# ---- data phase: forecast and news fallback overlap the gather --------------
events: list[str] = []


def yf_payload():
    return {
        "closes": [100.0] * 40, "highs": [101.0] * 40, "lows": [99.0] * 40,
        "volumes": [1000] * 40, "price": 100.0, "name": "Test Corp",
        "fundamentals": {}, "news": [{"title": "yf news"}],
    }


async def fake_yf_all(ticker):
    events.append("yf-start")
    await asyncio.sleep(0.05)
    events.append("yf-done")
    return yf_payload()


async def fake_fundamentals(ticker):
    events.append("fund-start")
    await asyncio.sleep(0.15)
    events.append("fund-done")
    return {}


async def fake_finnhub_news(ticker):
    events.append("fhnews-start")
    await asyncio.sleep(0.02)
    events.append("fhnews-done")
    return []


async def fake_olostep_news(ticker, name):
    events.append("olostep-news-start")
    await asyncio.sleep(0.02)
    events.append("olostep-news-done")
    return [{"title": "fallback news"}]


async def fake_social(ticker):
    events.append("social-start")
    await asyncio.sleep(0.25)
    events.append("social-done")
    return []


async def fake_forecast(closes):
    events.append("forecast-start")
    await asyncio.sleep(0.02)
    events.append("forecast-done")
    return {"forecast_method": "bako-timesfm"}


originals = (
    market_data_mod._yf_all,
    market_data_mod._finnhub_fundamentals,
    market_data_mod._finnhub_news,
    market_data_mod._olostep_news,
    market_data_mod._olostep_social,
)
market_data_mod._yf_all, market_data_mod._finnhub_fundamentals = fake_yf_all, fake_fundamentals
market_data_mod._finnhub_news = fake_finnhub_news
market_data_mod._olostep_news, market_data_mod._olostep_social = fake_olostep_news, fake_social
try:
    data = asyncio.run(market_data_mod.get_stock_data("TST", forecast=fake_forecast))
finally:
    (
        market_data_mod._yf_all,
        market_data_mod._finnhub_fundamentals,
        market_data_mod._finnhub_news,
        market_data_mod._olostep_news,
        market_data_mod._olostep_social,
    ) = originals

assert data.hosted_forecast == {"forecast_method": "bako-timesfm"}, data.hosted_forecast
assert data.sources["news"] == "olostep" and data.news[0]["title"] == "fallback news"
# The forecast starts once history lands, before the slow fundamentals fetch
# resolves; the news fallback starts while the slow social fetch is running.
assert events.index("forecast-start") < events.index("fund-done"), events
assert events.index("olostep-news-start") < events.index("social-done"), events
print("overlap OK: forecast starts after history, fallback before the gather ends")

# Finnhub returning news must suppress the fallback entirely.
events.clear()
market_data_mod._yf_all, market_data_mod._finnhub_fundamentals = fake_yf_all, fake_fundamentals


async def fake_finnhub_news_full(ticker):
    return [{"title": "finnhub news"}]


market_data_mod._finnhub_news = fake_finnhub_news_full
market_data_mod._olostep_news, market_data_mod._olostep_social = fake_olostep_news, fake_social
try:
    data = asyncio.run(market_data_mod.get_stock_data("TST", forecast=fake_forecast))
finally:
    (
        market_data_mod._yf_all,
        market_data_mod._finnhub_fundamentals,
        market_data_mod._finnhub_news,
        market_data_mod._olostep_news,
        market_data_mod._olostep_social,
    ) = originals
assert data.sources["news"] == "finnhub" and "olostep-news-start" not in events, events
print("fallback gate OK: no Olostep news call when Finnhub answers")

# ---- per-stage timings on a full offline replay ------------------------------
from app.workflow import analyze_ticker, over_budget  # noqa: E402
from app.tools.market_data import MarketData  # noqa: E402

assert over_budget("data_fetch", 61) and not over_budget("data_fetch", 59)
assert over_budget("sentiment", 181) and over_budget("manager", 241)

random.seed(11)
_closes = [100.0]
for _ in range(129):
    _closes.append(_closes[-1] * (1 + random.uniform(-0.015, 0.02)))
_snapshot = MarketData(
    ticker="NVDA",
    price=_closes[-1],
    company_name="Nvidia Corp",
    closes=_closes,
    highs=[c * 1.01 for c in _closes],
    lows=[c * 0.99 for c in _closes],
    volumes=[1_000_000.0] * len(_closes),
    fundamentals={"pe_ratio_ttm": 35.0},
    news=[{"title": "Guidance raised", "source": "finnhub", "summary": "beat",
           "url": "", "published": ""}],
    social=[],
    sources={"prices": "yfinance", "fundamentals": "none",
             "news": "none", "social": "none"},
    as_of="2026-09-05T20:00:00+00:00",
)


async def _noop(kind, payload):  # noqa: ARG001
    pass


_result = asyncio.run(
    asyncio.wait_for(
        analyze_ticker(
            "NVDA",
            _noop,
            market_data=_snapshot,
            live_context=False,
            exclude_analysts=("market", "fundamental", "news", "sentiment"),
            depth="pro",
        ),
        90,
    )
)
_stages = _result.stage_timings_s
for key in ("data_fetch", "technical", "forecast", "debate", "manager"):
    assert key in _stages, _stages
assert _stages["data_fetch"] == 0.0  # injected snapshot: no fetch to time
assert all(isinstance(v, (int, float)) and v >= 0 for v in _stages.values())
print("stage timings OK:", _stages)

# ---- workflow imports (catches syntax / import errors everywhere) ------------
import app.main  # noqa: E402, F401

print("app.main import OK")
print("ALL CHECKS PASSED")
