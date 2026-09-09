"""Offline checks for the backtest harness (ROADMAP 2.2 + P1.2).

Run: uv run python scripts/check_backtest.py
All network functions are monkeypatched with synthetic series - the e2e runs
the real pipeline (mock agents) against synthetic point-in-time snapshots.
Covers the honest-experiment additions: excluded-by-default fundamentals,
current-vintage opt-in, baselines, holdout split, bootstrap intervals,
reproducibility manifests, and paired one-change comparisons.
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
from app.backtest.data import (  # noqa: E402
    _apply_fundamentals_mode,
    build_snapshot,
    published_on_or_before,
)
from app.backtest.experiments import parse_pair  # noqa: E402
from app.backtest.grade import (  # noqa: E402
    Decision,
    aggregate,
    bootstrap_mean_ci,
    buy_hold_pct,
    cumulative_return_pct,
    grade,
    max_drawdown_pct,
    momentum_decision,
    momentum_signal,
    period_stats,
    promotion_verdict,
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
assert build_flags("mock")["fundamentals_vintage"] == "excluded"
assert "excluded from replay" in build_flags("mock")["fundamentals_bias"]
assert "LOOK-AHEAD BIAS" in build_flags("mock", "current")["fundamentals_bias"]
print("report flags OK")

# ---- fundamentals vintage views (P1.2) ---------------------------------------------
cached_payload = {
    "history": {"price": 1.0},
    "fundamentals": {"pe_ratio_ttm": 20.0},
    "company_name": "NVIDIA",
    "sources": {"fundamentals": "finnhub"},
}
stripped = _apply_fundamentals_mode(cached_payload, "excluded")
assert stripped["fundamentals"] == {} and stripped["sources"]["fundamentals"] == "excluded"
assert stripped["company_name"] == "NVIDIA"  # only the metrics are dropped
assert _apply_fundamentals_mode(cached_payload, "current") == cached_payload
assert cached_payload["fundamentals"] == {"pe_ratio_ttm": 20.0}  # view never mutates input
print("fundamentals vintage views OK")

# ---- momentum baseline signal ------------------------------------------------------
rising = [100.0 + i * 0.5 for i in range(130)]
falling = [200.0 - i * 0.3 for i in range(130)]
assert momentum_signal(rising) is not None and momentum_signal(rising) > 0
assert momentum_signal(falling) is not None and momentum_signal(falling) < 0
assert momentum_signal([100.0] * 50) is None  # not enough bars
assert momentum_decision(rising) == "BUY" and momentum_decision(falling) == "HOLD"
print("momentum baseline OK:", round(momentum_signal(rising), 2))

# ---- bootstrap intervals and promotion verdicts ------------------------------------
values = [1.0, 2.0, 3.0, 4.0, 5.0]
ci_a = bootstrap_mean_ci(values, seed=7)
ci_b = bootstrap_mean_ci(values, seed=7)
assert ci_a == ci_b, "same seed must reproduce the same interval"
assert ci_a[0] <= 3.0 <= ci_a[1], "the interval must bracket the mean"
ci_c = bootstrap_mean_ci(values, seed=8)
assert ci_c[0] <= 3.0 <= ci_c[1]
assert bootstrap_mean_ci([4.0], seed=7) is None
assert promotion_verdict(0, None) == "no positioned decisions"
assert "insufficient sample" in promotion_verdict(29, ci_a)
assert "crosses 0" in promotion_verdict(30, (-1.0, 1.0))
assert "excludes 0" in promotion_verdict(30, (0.5, 2.0))
stats = period_stats(
    [grade(Decision("A", "2024-01-01", "BUY", 0.8), CLOSES, SPY, 21)], 21
)
assert stats["positioned_n"] == 1 and stats["verdict"].startswith("insufficient sample")
print("bootstrap + verdicts OK:", ci_a)

# ---- paired spec parsing -----------------------------------------------------------
assert parse_pair("depth=fast:expert") == ("depth", "fast", "expert")
assert parse_pair("rebuttals=1:2") == ("rebuttals", "1", "2")
for bad in ("depth=fast", "depth=fast:fast", "vibes=1:2", "depth"):
    try:
        parse_pair(bad)
        raise AssertionError(f"parse_pair accepted {bad!r}")
    except ValueError:
        pass
print("paired spec parsing OK")

# ---- snapshot cache: round-trip, warm re-run, offline cold miss --------------------
cache = SnapshotCache(tmp / "cache.db")
cache.put_snapshot("RT", "2024-01-10", 183, 14, {"history": {"price": 1.0}})
assert cache.get_snapshot("RT", "2024-01-10", 183, 14) == {"history": {"price": 1.0}}
cache.put_fundamentals("RT", {"company_name": "RoundTrip"})
assert cache.get_fundamentals("RT")["company_name"] == "RoundTrip"
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
GRID_DAYS = [START, (GRID_START + timedelta(days=21)).isoformat(), END]
END_CHECKS = {"mid": GRID_DAYS[1]}


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

    # honest default (P1.2): no fundamentals anywhere in the replay
    assert result.config["fundamentals"] == "excluded"
    assert result.config["excluded_analysts"] == []  # fast depth has no fundamental analyst
    assert calls["fund"] == 0, "excluded mode must not fetch fundamentals"

    # every decision is in the report, and the future news item never leaked
    payload = json.loads((OUT_DIR / "report-mock.json").read_text())
    assert len(payload["outcomes"]) == 6
    assert payload["flags"]["memorization_risk"].startswith("low")
    assert payload["flags"]["fundamentals_vintage"] == "excluded"
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

    # baselines ship with the report (P1.2)
    baselines = payload["overall"]["baselines"]
    assert baselines["all_hold"]["decisions"] == 6
    assert baselines["momentum"]["decisions"] == 6
    assert baselines["momentum"]["positioned_n"] >= 1, "rising synthetic series must trigger momentum BUYs"
    report_md = (OUT_DIR / "report-mock.md").read_text()
    assert "all HOLD" in report_md and "deterministic momentum" in report_md
    print("baselines OK: momentum positioned", baselines["momentum"]["positioned_n"])

    # manifest beside the report: revision, policy version, per-snapshot hashes
    manifest = json.loads((OUT_DIR / "manifest-mock.json").read_text())
    assert manifest["code"]["revision"], "git revision must be recorded"
    assert manifest["decision_policy_version"]
    assert set(manifest["data"]["snapshot_hashes"]) == {
        f"{t}@{d}" for t in ("NVDA", "AMD") for d in (START, END_CHECKS["mid"], END)
    }
    assert manifest["random_seeds"]["bootstrap"] == 7
    first_manifest = {k: v for k, v in manifest.items() if k != "generated_at"}
    print("manifest OK:", len(manifest["data"]["snapshot_hashes"]), "snapshot hashes")

    # warm re-run: identical results, zero new network calls, identical manifest
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
    assert [vars(o) for o in result2.outcomes] == [vars(o) for o in result.outcomes]
    manifest2 = json.loads((OUT_DIR / "manifest-mock.json").read_text())
    assert {k: v for k, v in manifest2.items() if k != "generated_at"} == first_manifest, \
        "same cache and code must reproduce the same manifest"
    print("warm re-run OK: zero network calls, identical results and manifest")

    # medium depth asks for the fundamental analyst; the honest default still
    # excludes it because no point-in-time fundamentals source exists
    medium = await run_backtest(
        tickers=["NVDA"], start=START, end=START, step_days=21, horizon_days=21,
        depth="medium", mode="mock", out_dir=OUT_DIR, cache=cache,
        name="medium-excluded",
    )
    assert medium.config["excluded_analysts"] == ["fundamental"]
    assert calls["fund"] == 0
    medium_payload = json.loads((OUT_DIR / "report-medium-excluded.json").read_text())
    assert medium_payload["flags"]["fundamentals_vintage"] == "excluded"
    print("medium-depth exclusion OK: fundamental analyst dropped, zero fetches")

    # opting back in upgrades the cached snapshots and says so loudly
    current = await run_backtest(
        tickers=["NVDA"], start=START, end=START, step_days=21, horizon_days=21,
        depth="medium", mode="mock", out_dir=OUT_DIR, cache=cache,
        fundamentals="current", name="medium-current",
    )
    assert current.config["fundamentals"] == "current"
    assert calls["fund"] == 1, "current mode fetches fundamentals exactly once"
    upgraded = cache.get_snapshot("NVDA", START, 183, 14)
    assert upgraded["fundamentals"].get("pe_ratio_ttm") == 20.0
    current_md = (OUT_DIR / "report-medium-current.md").read_text()
    assert "WARNING: current-vintage fundamentals" in current_md
    print("current-vintage opt-in OK: warning present, snapshot upgraded from cache")

    # holdout split: periods sum to the whole grid and the report shows verdicts
    holdout_day = END_CHECKS["mid"]
    split = await run_backtest(
        tickers=["NVDA", "AMD"], start=START, end=END, step_days=21, horizon_days=21,
        depth="fast", mode="mock", out_dir=OUT_DIR, cache=cache,
        holdout=holdout_day, name="split",
    )
    periods = split.overall["periods"]
    assert periods["tune"]["decisions"] + periods["test"]["decisions"] == 6
    assert periods["test"]["decisions"] == 4, "two tickers x two dates >= holdout"
    assert "insufficient sample" in periods["test"]["verdict"]
    split_md = (OUT_DIR / "report-split.md").read_text()
    assert "Tune / test split" in split_md and "test verdict" in split_md
    print("holdout split OK: tune", periods["tune"]["decisions"], "/ test", periods["test"]["decisions"])

    # paired one-change comparison: shared cache, verdict refuses thin samples
    from app.backtest.experiments import run_paired

    _, _, paired_path = await run_paired(
        "depth", "fast", "medium",
        tickers=["NVDA"], start=START, end=START, step_days=21, horizon_days=21,
        mode="mock", out_dir=OUT_DIR, cache=cache,
    )
    paired_md = paired_path.read_text()
    assert "Only `depth` differs" in paired_md
    assert "insufficient paired sample" in paired_md, paired_md
    assert (OUT_DIR / "report-mock-depth-left.md").exists()
    assert (OUT_DIR / "report-mock-depth-right.md").exists()
    print("paired experiment OK:", paired_path.name)

    # backtest replays never write to the live decision memory
    with memory._connect() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    assert rows == 0, rows
    print("decision memory untouched OK")


asyncio.run(e2e())
print("ALL BACKTEST CHECKS PASSED")
