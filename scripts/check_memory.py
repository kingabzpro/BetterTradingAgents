"""Offline checks for decision memory (ROADMAP 1.1). Run: PYTHONPATH=. uv run python scripts/check_memory.py

Grading is tested against synthetic closes (no network); the mock e2e at the
end hits yfinance once, like scripts/check_risk.py does.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "memory_test.db")

from app import memory  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import StockAnalysis  # noqa: E402

settings.llm_api_key = ""  # force mock mode for the e2e part

TODAY = datetime.now(timezone.utc).date()
D30 = (TODAY - timedelta(days=30)).isoformat()
D21 = (TODAY - timedelta(days=9)).isoformat()
D5 = (TODAY - timedelta(days=5)).isoformat()
TODAY_ISO = TODAY.isoformat()

# ---- deterministic lesson wording --------------------------------------------
assert "beat the market" in memory.lesson("BUY", 5.0, 3.0)
assert "lagged the market" in memory.lesson("BUY", -5.0, -3.0)
assert "matched" in memory.lesson("BUY", 1.0, 0.2)
assert "right" in memory.lesson("SELL", -6.0, -4.0)
assert "wrong" in memory.lesson("SELL", 6.0, 4.0)
assert "avoided" in memory.lesson("HOLD", -4.0, -1.0)
assert "missed" in memory.lesson("HOLD", 5.0, 1.0)
assert "cost little" in memory.lesson("HOLD", 0.5, 0.0)
print("lesson wording OK")


def make_analysis(ticker, decision, price, confidence=0.8):
    return StockAnalysis(
        ticker=ticker,
        decision=decision,
        confidence=confidence,
        price=price,
        summary=f"{decision} on {ticker} (test seed)",
        bull_case="bull",
        bear_case="bear",
    )


# ---- outcome computation on synthetic closes ----------------------------------
# BUY decided 30d ago: +10% over the 21-day window vs SPY +1% -> alpha +9%.
row30 = {
    "id": 1,
    "ticker": "NVDA",
    "date": D30,
    "decision": "BUY",
    "confidence": 0.8,
    "price_at_decision": 100.0,
    "summary": "s",
}
CLOSES = {D30: 100.0, D21: 110.0, TODAY_ISO: 120.0}
SPY = {D30: 400.0, D21: 404.0, TODAY_ISO: 408.0}
out = memory.compute_outcome(row30, CLOSES, SPY)
assert out["mature"] is True, out
assert out["window_days"] == 21, out
assert abs(out["realized_return_pct"] - 10.0) < 0.01, out
assert abs(out["spy_return_pct"] - 1.0) < 0.01, out
assert abs(out["alpha_vs_spy_pct"] - 9.0) < 0.01, out

# Decided today: entry exists but no later close -> ungradable.
today_row = {**row30, "date": TODAY_ISO}
assert memory.compute_outcome(today_row, CLOSES, SPY) is None
print("outcome math OK: +10% vs +1% over 21d, decided-today returns None")


async def main():
    await memory.init()

    async def mature_pair(ticker, start):
        return dict(CLOSES), dict(SPY)

    # ---- mature decision: graded, stored, and computed exactly once ----------
    memory._fetch_closes_pair = mature_pair
    await memory.record_decision("seedrun", make_analysis("NVDA", "BUY", 100.0), decision_date=D30)
    refl = await memory.get_reflections("NVDA")
    assert refl["past_decisions"], refl
    first = refl["past_decisions"][0]
    assert first["realized_return_pct"] == 10.0 and first["alpha_vs_spy_pct"] == 9.0, first
    assert first["mature"] is True and "beat the market" in first["reflection"], first
    print("mature grading OK:", first["reflection"])

    async def broken_pair(ticker, start):
        raise RuntimeError("network down")

    memory._fetch_closes_pair = broken_pair
    refl_again = await memory.get_reflections("NVDA")
    # The mature row is read from storage without any price fetch.
    assert refl_again["past_decisions"][0]["realized_return_pct"] == 10.0
    print("computed-once storage OK")

    # ---- young decision: partial on-the-fly grade, never stored --------------
    memory._fetch_closes_pair = mature_pair
    await memory.record_decision("seedrun2", make_analysis("NVDA", "HOLD", 100.0), decision_date=D5)
    partial_closes = {D5: 100.0, TODAY_ISO: 103.0}
    partial_spy = {D5: 400.0, TODAY_ISO: 406.0}

    async def partial_pair(ticker, start):
        return dict(partial_closes), dict(partial_spy)

    memory._fetch_closes_pair = partial_pair
    refl = await memory.get_reflections("NVDA")
    young = refl["past_decisions"][0]  # newest first
    assert young["date"] == D5 and young["decision"] == "HOLD", young
    assert young["realized_return_pct"] == 3.0 and young["mature"] is False, young
    assert young["window_days"] == 5 and "partial window" in young["reflection"], young
    assert "missed" in young["reflection"], young
    with memory._connect() as conn:
        still_open = conn.execute(
            "SELECT mature FROM decisions WHERE date = ? AND ticker = 'NVDA'", (D5,)
        ).fetchone()["mature"]
    assert still_open == 0, "young decision must not be frozen as mature"
    print("partial grading OK:", young["reflection"])

    # ---- cross-ticker lessons: stored outcomes from other tickers only -------
    memory._fetch_closes_pair = mature_pair
    await memory.record_decision("seedamd", make_analysis("AMD", "SELL", 100.0), decision_date=D30)
    await memory.get_reflections("AMD")  # grades + stores the AMD row

    async def dead_pair(ticker, start):
        return {}, {}  # what a dead network looks like after the internal retry

    memory._fetch_closes_pair = dead_pair
    refl = await memory.get_reflections("NVDA")
    cross = refl["cross_ticker_lessons"]
    assert len(cross) == 1 and cross[0]["ticker"] == "AMD", cross
    assert cross[0]["realized_return_pct"] == 10.0, cross
    assert all(r["ticker"] == "NVDA" for r in refl["past_decisions"])
    print("cross-ticker lessons OK:", cross[0]["reflection"])

    # ---- e2e (mock agents, real yfinance): record + inject -------------------
    from app.tools.market_data import get_closes_between

    async def real_pair(ticker, start):
        end = (TODAY + timedelta(days=1)).isoformat()
        own = await get_closes_between(ticker, start, end)
        spy_closes = await get_closes_between("SPY", start, end)
        return own, spy_closes

    memory._fetch_closes_pair = real_pair
    from app.workflow import analyze_ticker

    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    result = await asyncio.wait_for(analyze_ticker("NVDA", emit, run_id="e2e1"), timeout=120)
    assert result.error is None, result.error
    assert result.past_decisions, "second run must see past decisions"
    graded = [r for r in result.past_decisions if r["realized_return_pct"] is not None]
    assert graded and any(r["alpha_vs_spy_pct"] is not None for r in graded), result.past_decisions
    with memory._connect() as conn:
        row = conn.execute(
            "SELECT decision, confidence, run_id FROM decisions WHERE run_id = 'e2e1'"
        ).fetchone()
    assert row and row["decision"] == result.decision and row["run_id"] == "e2e1", row
    completed = [p for kind, p in events if kind == "ticker_completed" and not p.get("error")]
    assert len(completed) == 1
    print("e2e run 1 OK:", result.decision, "| past_decisions:",
          [(r["decision"], r["realized_return_pct"]) for r in result.past_decisions])

    result2 = await asyncio.wait_for(analyze_ticker("NVDA", emit, run_id="e2e2"), timeout=120)
    assert result2.error is None, result2.error
    with memory._connect() as conn:
        rows = conn.execute(
            "SELECT run_id FROM decisions WHERE run_id IN ('e2e1','e2e2')"
        ).fetchall()
    assert len(rows) == 2, "a decision row must exist for every completed run"
    print("e2e run 2 OK: 2 decision rows for 2 completed runs")


asyncio.run(main())
print("ALL MEMORY CHECKS PASSED")
