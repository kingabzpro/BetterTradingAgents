"""Offline checks for the sentiment/social analyst (ROADMAP 3.1).

Run: PYTHONPATH=. uv run python scripts/check_sentiment.py
No network: the e2e injects an in-memory MarketData snapshot (the same seam the
backtester uses), so the whole pipeline runs in mock mode hermetically.
"""

import asyncio
import os
import random
import tempfile
from pathlib import Path

# Isolated DB + no provider keys before app.config is imported.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "sentiment_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""

from app.agents import sentiment  # noqa: E402
from app.depth import depth_profile  # noqa: E402
from app.tools.market_data import MarketData  # noqa: E402
from app.workflow import _source_references, analyze_ticker  # noqa: E402

# ---- mock scoring: thick crowds read, thin crowds don't ------------------------
bullish_posts = [
    {"title": "NVDA to the moon", "summary": "earnings beat, this rally is just starting"},
    {"title": "Loading up on NVDA calls", "summary": "gains keep compounding, strong momentum"},
    {"title": "DD: NVDA still undervalued", "summary": "record datacenter growth, bullish"},
    {"title": "NVDA squeeze incoming", "summary": "everyone holding, rocket emoji"},
]
bearish_posts = [
    {"title": "NVDA bagholders incoming", "summary": "valuation is a scam, dump imminent"},
    {"title": "Taking profits on NVDA", "summary": "weak guidance, fears of a plunge"},
    {"title": "NVDA puts printing", "summary": "bearish breakdown, losses everywhere"},
    {"title": "Tanking after hours", "summary": "downgrade, bearish sentiment"},
]

bull = sentiment.mock("NVDA", {"items": bullish_posts})
assert bull["signal"] == "positive" and bull["confidence"] == 0.6, bull
bear = sentiment.mock("NVDA", {"items": bearish_posts})
assert bear["signal"] == "negative", bear
mixed = sentiment.mock("NVDA", {"items": bullish_posts[:2] + bearish_posts[:2]})
assert mixed["signal"] == "neutral", mixed
print("mock scoring OK:", bull["signal"], "/", bear["signal"], "/", mixed["signal"])

# ---- thin social volume -> neutral with low confidence -------------------------
for items in ([], bullish_posts[:2]):
    thin = sentiment.mock("NVDA", {"items": items})
    assert thin["signal"] == "neutral" and thin["confidence"] <= 0.4, thin
    assert "too thin" in thin["summary"], thin
assert sentiment.MIN_ITEMS == 3
print("thin-volume brake OK:", len([]), "and", 2, "posts -> neutral 0.35")

# ---- to_result coercion --------------------------------------------------------
result = sentiment.to_result(
    {"signal": "VERY-BULLISH", "confidence": 83, "summary": "x" * 900}, "NVDA"
)
assert result.agent == "sentiment"
assert result.signal == "neutral", result.signal  # unknown signal coerces, not guessed
assert result.confidence == 0.83 and len(result.summary) <= 501
ok = sentiment.to_result({"signal": "negative", "confidence": 0.7, "summary": "sour"}, "AMD")
assert ok.signal == "negative" and ok.confidence == 0.7
print("to_result OK: coercion + clipping")

# ---- depth profiles: 5th researcher on medium/expert ---------------------------
assert "sentiment" in depth_profile("medium")["research"]
assert "sentiment" in depth_profile("expert")["research"]
assert "sentiment" not in depth_profile("fast")["research"]
print("depth profiles OK: sentiment runs at medium/expert, not fast")

# ---- source references include social posts ------------------------------------
market = MarketData(
    ticker="NVDA",
    news=[{"title": "News headline", "source": "finnhub", "url": "https://example.com/n",
           "published": "2026-09-01"}],
    social=[
        {"title": "Reddit thread", "source": "reddit.com", "url": "https://reddit.com/r/x"},
        {"title": "StockTwits post", "source": "stocktwits.com", "url": "https://stocktwits.com/y"},
        {"title": "", "source": "z", "url": ""},  # no title -> dropped
    ],
    sources={"news": "finnhub", "social": "olostep"},
)
refs = _source_references(market)
kinds = [r.kind for r in refs]
assert kinds == ["news", "social", "social"], kinds
assert refs[1].provider == "reddit.com" and refs[2].url == "https://stocktwits.com/y"
print("source references OK:", kinds)


# ---- hermetic e2e: full mock pipeline with an injected snapshot -----------------
def snapshot(ticker: str, social: list[dict]) -> MarketData:
    random.seed(11)
    closes = [100.0]
    for _ in range(129):
        closes.append(closes[-1] * (1 + random.uniform(-0.015, 0.02)))
    return MarketData(
        ticker=ticker,
        price=closes[-1],
        company_name="Nvidia Corp",
        closes=closes,
        highs=[c * 1.01 for c in closes],
        lows=[c * 0.99 for c in closes],
        volumes=[1_000_000.0] * len(closes),
        fundamentals={"pe_ratio_ttm": 35.0, "revenue_growth_ttm_pct": 60.0},
        news=[{"title": "Guidance raised", "source": "finnhub", "published": "",
               "summary": "beat and raise", "url": "https://example.com/g"}],
        social=social,
        sources={"prices": "yfinance", "fundamentals": "finnhub",
                 "news": "finnhub", "social": "olostep" if social else "none"},
        as_of="2026-09-04T20:00:00+00:00",
    )


async def e2e():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    # Thick crowd: the sentiment agent must run, complete and reach the result.
    result = await asyncio.wait_for(
        analyze_ticker("NVDA", emit, market_data=snapshot("NVDA", bullish_posts),
                       live_context=False),
        timeout=90,
    )
    assert result.error is None, result.error
    assert result.sentiment is not None, "sentiment agent did not run"
    assert result.sentiment.agent == "sentiment" and result.sentiment.signal == "positive"
    assert result.providers["social"] == "olostep", result.providers
    agents = [p["agent"] for kind, p in events if kind == "agent_started"]
    assert "sentiment" in agents, agents
    social_refs = [r for r in result.source_references if r.kind == "social"]
    assert social_refs and social_refs[0].title == bullish_posts[0]["title"]
    print("e2e (thick crowd) OK:", result.decision, "| sentiment",
          result.sentiment.signal, f"{result.sentiment.confidence:.2f}")

    # Backtest-style empty social set -> thin-volume neutral, run still completes.
    quiet = await asyncio.wait_for(
        analyze_ticker("AMD", emit, market_data=snapshot("AMD", []),
                       live_context=False),
        timeout=90,
    )
    assert quiet.error is None, quiet.error
    assert quiet.sentiment is not None and quiet.sentiment.signal == "neutral"
    assert quiet.sentiment.confidence <= 0.4, quiet.sentiment
    assert quiet.providers["social"] == "none"
    assert "too thin" in quiet.sentiment.summary
    print("e2e (thin crowd) OK:", quiet.decision, "| sentiment neutral",
          f"{quiet.sentiment.confidence:.2f}")


asyncio.run(e2e())
print("ALL SENTIMENT CHECKS PASSED")
