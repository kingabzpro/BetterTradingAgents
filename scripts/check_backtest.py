"""Offline checks for the backtest harness (ROADMAP 2.2).

Run: PYTHONPATH=. uv run python scripts/check_backtest.py
All network functions are monkeypatched with synthetic series - the e2e runs
the real pipeline (mock agents) against synthetic point-in-time snapshots.
"""

import asyncio
import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(tmp / "backtest_test.db")
os.environ["BACKTEST_CACHE"] = str(tmp / "cache.db")
os.environ["LLM_API_KEY"] = ""  # mock mode

from app import memory  # noqa: E402
from app.backtest import data as bdata, run as brun  # noqa: E402
from app.backtest.cache import SnapshotCache  # noqa: E402
from app.backtest.data import build_snapshot, published_on_or_before  # noqa: E402
from app.backtest.grade import (  # noqa: E402
    Decision,
    aggregate,
    buy_hold_pct,
    cumulative_return_pct,
    grade,
    max_drawdown_pct,
    sharpe,
)
from app.backtest.run import date_grid  # noqa: E402
from app.backtest.report import build_flags  # noqa: E402
from app.config import settings  # noqa: E402

settings.llm_api_key = ""

# ---- date grid ----------------------------------------------------------------
assert date_grid("2024-01-01", "2024-02-01", 21) == ["2024-01-01", "2024-01-22"]
assert date_grid("2024-01-01", "2024-01-21", 21) == ["2024-01-01"]
print("date grid OK")

# ---- grading against hand-computed windows -------------------------------------
CLOSES = {"2024-01-01": 100.0, "2024-01-22": 110.0, "2024-02-12": 99.0, "2024-03-04": 105.0}
SPY = {"2024-01-01": 400.0, "2024-01-22": 402.0, "2024-02-12": 404.0, "2024-03-04": 406.0}

out = grade(Decision("NVDA", "2024-01-01", "BUY", 0.8), CLOSES, SPY, 21)
assert out and (out.entry, out.exit, out.window_days) == (100.0, 110.0, 21), out
assert out.gross_pct == 10.0 and out.net_pct == 9.9, out  # 10% - 2x5bp cost
assert out.spy_pct == 0.5 and out.alpha_pct == 9.4, out
print("BUY grading OK: net", out.net_pct, "alpha", out.alpha_pct)

out = grade(Decision("NVDA", "2024-01-22", "SELL", 0.7), CLOSES, SPY, 21, short=True)
assert out.net_pct == 9.9 and out.alpha_pct == 9.4, out  # stock fell 10%, short wins
print("short SELL grading OK:", out.net_pct)

out = grade(Decision("NVDA", "2024-01-22", "SELL", 0.7), CLOSES, SPY, 21, short=False)
assert out.net_pct == 0.0 and out.alpha_pct is None and "long-only" in out.note, out
print("long-only SELL grading OK: scores 0, no alpha")

out = grade(Decision("NVDA", "2024-02-12", "HOLD", 0.5), CLOSES, SPY, 21)
assert out.net_pct == 0.0 and out.gross_pct == 6.06 and out.alpha_pct is None, out
assert grade(Decision("NVDA", "2024-03-04", "BUY", 0.8), CLOSES, SPY, 21) is None
print("HOLD + ungraded-window OK")

# ---- aggregate math -------------------------------------------------------------
assert cumulative_return_pct([10.0, -5.0]) == 4.5
assert max_drawdown_pct([10.0, -20.0, 15.0]) == 20.0
assert max_drawdown_pct([5.0, 5.0]) == 0.0
assert sharpe([1.0, 2.0, 3.0], 12.0) == 8.49
assert sharpe([5.0, 5.0], 12.0) == 0.0
metrics = aggregate(
    [
        grade(Decision("A", "2024-01-01", "BUY", 0.8), CLOSES, SPY, 21),
        grade(Decision("A", "2024-01-22", "BUY", 0.8), CLOSES, SPY, 21),
    ],
    21,
)
# window 1: 100 -> 110 = +9.9 net; window 2: 110 -> 99 = -10.1 net
assert metrics["hit_rate_pct"] == 50.0, metrics
assert metrics["cumulative_pct"] == -1.2, metrics  # 1.099 * 0.899 - 1
assert metrics["avg_net_pct"] == -0.1 and metrics["avg_alpha_pct"] == -0.6, metrics
assert metrics["counts"] == {"BUY": 2, "SELL": 0, "HOLD": 0}
one = grade(Decision("A", "2024-01-01", "BUY", 0.8), CLOSES, SPY, 21)
two = grade(Decision("A", "2024-01-22", "BUY", 0.8), CLOSES, SPY, 21)
assert buy_hold_pct(CLOSES, [one]) == 10.0  # 100 -> 110 (that outcome's exit)
assert buy_hold_pct(CLOSES, [one, two]) == -1.0  # 100 -> 99 across both windows
print("aggregate math OK: cum", metrics["cumulative_pct"], "hit", metrics["hit_rate_pct"])

# ---- anti-look-ahead news filter -------------------------------------------------
assert published_on_or_before({"published": "2024-01-05T10:00:00+00:00"}, "2024-01-10")
assert not published_on_or_before({"published": "2024-01-15T10:00:00+00:00"}, "2024-01-10")
assert not published_on_or_before({"published": ""}, "2024-01-10")
print("anti-look-ahead news filter OK")

# ---- report flags -----------------------------------------------------------------
assert build_flags("llm")["memorization_risk"] == "high"
assert build_flags("mock")["memorization_risk"].startswith("low")
print("report flags OK")

# ---- snapshot cache: round-trip, warm re-run, offline cold miss --------------------
cache = SnapshotCache(tmp / "cache.db")
cache.put_snapshot("NVDA", "2024-01-10", 183, 14, {"history": {"price": 1.0}})
assert cache.get_snapshot("NVDA", "2024-01-10", 183, 14) == {"history": {"price": 1.0}}
cache.put_fundamentals("NVDA", {"company_name": "NVIDIA"})
assert cache.get_fundamentals("NVDA")["company_name"] == "NVIDIA"
cache.put_series("SPY", "a", "b", {"2024-01-01": 400.0})
assert cache.get_series("SPY", "a", "b") == {"2024-01-01": 400.0}
print("cache round-trip OK")

calls = {"ohlcv": 0, "news": 0, "fund": 0, "closes": 0}

TODAY = date.today()
GRID_START = TODAY - timedelta(days=90)


def synthetic_closes(ticker: str, start: str, end: str) -> dict[str, float]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    out = {}
    i = 0
    day = first
    while day <= last:
        if day.weekday() < 5:  # trading days only
            base = 100.0 if ticker == "SPY" else 50.0
            out[day.isoformat()] = round(base + i * 0.05 + (2.0 if i % 5 == 0 else -1.0), 4)
        day += timedelta(days=1)
        i += 1
    return out


async def fake_ohlcv(ticker, start, end):
    calls["ohlcv"] += 1
    closes = synthetic_closes(ticker, start, end)
    return {
        "closes": list(closes.values()),
        "highs": [c * 1.01 for c in closes.values()],
        "lows": [c * 0.99 for c in closes.values()],
        "volumes": [1_000_000] * len(closes),
        "price": list(closes.values())[-1] if closes else None,
    }


async def fake_news(ticker, from_date, to_date):
    calls["news"] += 1
    return [
        {"title": "strong growth record", "published": f"{to_date}T09:00:00+00:00"},
        {"title": "future item", "published": "2099-01-01T00:00:00+00:00"},
    ]


async def fake_fundamentals(ticker):
    calls["fund"] += 1
    return {"company_name": f"{ticker} Inc", "pe_ratio_ttm": 20.0}


bdata.get_ohlcv_between = fake_ohlcv
bdata.get_news_between = fake_news
bdata._finnhub_fundamentals = fake_fundamentals


async def fake_get_closes(ticker, start, end):
    calls["closes"] += 1
    return synthetic_closes(ticker, start, end)


brun.get_closes_between = fake_get_closes

from app.backtest.run import run_backtest  # noqa: E402

OUT_DIR = tmp / "reports"
START = GRID_START.isoformat()
END = (GRID_START + timedelta(days=42)).isoformat()  # 3 grid dates, all gradable


async def e2e():
    await memory.init()

    result = await run_backtest(
        tickers=["NVDA", "AMD"],
        start=START,
        end=END,
        step_days=21,
        horizon_days=21,
        depth="fast",
        mode="mock",
        out_dir=OUT_DIR,
        cache=cache,
    )
    assert len(result.outcomes) == 6, [(o.ticker, o.date) for o in result.outcomes]
    assert not result.ungraded, result.ungraded
    assert all(o.alpha_pct is not None for o in result.outcomes)
    assert result.overall["decisions"] == 6
    first_calls = dict(calls)

    # every decision is in the report, and the future news item never leaked
    payload = json.loads((OUT_DIR / "report-mock.json").read_text())
    assert len(payload["outcomes"]) == 6
    assert payload["flags"]["memorization_risk"].startswith("low")
    assert (OUT_DIR / "report-mock.md").read_text().startswith("# Backtest report")
    snapshot_news = json.loads(
        cache._connect()
        .execute(
            "SELECT payload FROM snapshots WHERE ticker = 'NVDA' AND as_of = ?",
            (START,),
        )
        .fetchone()["payload"]
    )["news"]
    assert len(snapshot_news) == 1 and snapshot_news[0]["title"] == "strong growth record"
    print("e2e run 1 OK:", result.overall["counts"], "| news filtered to 1 item")

    # warm re-run: identical results, zero new network calls
    result2 = await run_backtest(
        tickers=["NVDA", "AMD"],
        start=START,
        end=END,
        step_days=21,
        horizon_days=21,
        depth="fast",
        mode="mock",
        out_dir=OUT_DIR,
        cache=cache,
    )
    assert calls == first_calls, (calls, first_calls)
    assert result2.overall["cumulative_pct"] == result.overall["cumulative_pct"]
    print("warm re-run OK: zero network calls, identical results")

    # backtest replays never write to the live decision memory
    with memory._connect() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    assert rows == 0, rows
    print("decision memory untouched OK")


asyncio.run(e2e())
print("ALL BACKTEST CHECKS PASSED")
