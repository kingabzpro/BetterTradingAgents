"""Offline checks for the Accuracy report (ROADMAP P1.9).

Run: PYTHONPATH=. uv run python scripts/check_accuracy.py
No network: closes are stubbed and decisions are seeded directly with past
dates so every grading branch (right / wrong / neutral / pending / no data)
runs against known numbers.
"""

import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile

# Isolated DB before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "accuracy_test.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""

import httpx  # noqa: E402

from app import accuracy, memory  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import StockAnalysis  # noqa: E402
from app.tools import market_data  # noqa: E402

TODAY = datetime.now(timezone.utc).date()
OLD = TODAY - timedelta(days=settings.memory_horizon_days + 14)
MID = (OLD + timedelta(days=20)).isoformat()  # the graded exit close, inside the window
END = (OLD + timedelta(days=28)).isoformat()  # past the target: proves the window closed
# Ticker closes: decision-day 100 -> exit close; SPY +0.2 over the window.
CLOSES = {
    "AAAA": {OLD.isoformat(): 100.0, MID: 103.0, END: 104.0},  # BUY,  alpha +2.8 -> right
    "BBBB": {OLD.isoformat(): 100.0, MID: 90.0, END: 89.0},    # SELL, alpha -10.2 -> right
    "CCCC": {OLD.isoformat(): 100.0, MID: 92.0, END: 91.0},    # HOLD, realized -8 -> right (avoided)
    "DDDD": {OLD.isoformat(): 100.0, MID: 105.0, END: 106.0},  # HOLD, realized +5 -> wrong (missed)
    "EEEE": {OLD.isoformat(): 100.0, MID: 100.5, END: 100.6},  # BUY,  alpha +0.3 -> neutral
    "HHHH": {OLD.isoformat(): 100.0, MID: 85.0, END: 84.0},    # HOLD, realized -15 -> right (slide avoided)
    "SPY": {OLD.isoformat(): 100.0, MID: 100.2, END: 100.3},
}
FETCHED: list[str] = []


async def fake_closes(symbol: str, start: str, end: str):
    FETCHED.append(symbol)
    return dict(CLOSES.get(symbol, {}))  # GGGG: no data, stays pending


def analysis_for(ticker: str, decision: str) -> StockAnalysis:
    return StockAnalysis(
        ticker=ticker,
        company_name="Accuracy Check Co",
        price=100.0,
        decision=decision,  # type: ignore[arg-type]
        confidence=0.6,
        as_of="2026-01-01T12:00:00+00:00",
    )


async def seed() -> None:
    await memory.init()
    for ticker, decision in (
        ("AAAA", "BUY"), ("BBBB", "SELL"), ("CCCC", "HOLD"),
        ("DDDD", "HOLD"), ("EEEE", "BUY"), ("GGGG", "BUY"), ("HHHH", "HOLD"),
    ):
        await memory.record_decision("acc0000001", analysis_for(ticker, decision), OLD.isoformat())
    # A rerun of the same call on the same day: one call, must count once.
    await memory.record_decision("acc0000001", analysis_for("AAAA", "BUY"), OLD.isoformat())
    # Decided today: the window is open, so no price fetch may happen for it.
    await memory.record_decision("acc0000002", analysis_for("FFFF", "BUY"), TODAY.isoformat())


async def checks() -> None:
    # ---- verdict rule (shared with the benchmark scorecard) ------------------
    assert memory.verdict("BUY", 1.0, None) == "unknown"
    assert memory.verdict("SELL", 0.0, None) == "unknown"
    assert memory.verdict("BUY", 0.0, 3.0) == "right"
    assert memory.verdict("BUY", 0.0, -3.0) == "wrong"
    assert memory.verdict("SELL", 0.0, -3.0) == "right"
    assert memory.verdict("HOLD", -8.0, 0.0) == "right"
    assert memory.verdict("HOLD", 5.0, 0.0) == "wrong"
    assert memory.verdict("HOLD", 0.5, None) == "neutral"
    print("verdict rule OK")

    await seed()
    market_data.get_closes_between = fake_closes

    # ---- first report: grade, aggregate, count pending -----------------------
    report = await accuracy.accuracy_report()
    assert report["graded"] == 6 and report["pending"] == 2, report
    verdicts = {row["ticker"]: row["verdict"] for row in report["rows"]}
    assert verdicts == {
        "AAAA": "right", "BBBB": "right", "CCCC": "right",
        "DDDD": "wrong", "EEEE": "neutral", "HHHH": "right",
    }, verdicts
    # The duplicate AAAA rerun must not appear: one call counts once.
    assert len(report["rows"]) == 6 and len(verdicts) == 6
    # Newest first; the deduped AAAA keeps its newest id (the rerun row), so it leads.
    assert [row["ticker"] for row in report["rows"]] == [
        "AAAA", "HHHH", "EEEE", "DDDD", "CCCC", "BBBB",
    ]
    aaaa = next(row for row in report["rows"] if row["ticker"] == "AAAA")
    assert aaaa["realized_return_pct"] == 3.0 and aaaa["spy_return_pct"] == 0.2
    assert aaaa["alpha_vs_spy_pct"] == 2.8 and aaaa["entry_price"] == 100.0
    assert aaaa["lesson"], "each row must carry its one-line lesson"

    by = {group["decision"]: group for group in report["by_decision"]}
    assert by["BUY"] == {
        "decision": "BUY", "n": 2, "right": 1, "wrong": 0, "neutral": 1,
        "hit_rate": 1.0, "mean_alpha_pct": (2.8 + 0.3) / 2,
        "mean_realized_pct": (3.0 + 0.5) / 2,
    }, by["BUY"]
    assert by["SELL"]["n"] == 1 and by["SELL"]["hit_rate"] == 1.0
    assert by["HOLD"]["n"] == 3 and abs(by["HOLD"]["hit_rate"] - 2 / 3) < 1e-9
    assert abs(by["HOLD"]["mean_alpha_pct"] - (-8.2 + 4.8 - 15.2) / 3) < 1e-9
    assert abs(by["HOLD"]["mean_realized_pct"] - (-8.0 + 5.0 - 15.0) / 3) < 1e-9

    # Per-signal: long-only following (BUYs ride, others cash) vs always-buy vs SPY.
    per_signal = report["per_signal"]
    assert per_signal["n"] == 6
    assert abs(per_signal["follow_calls_pct"] - (3.0 + 0.5) / 6) < 1e-9
    assert abs(per_signal["always_buy_pct"] - (3.0 - 10.0 - 8.0 + 5.0 + 0.5 - 15.0) / 6) < 1e-9
    assert abs(per_signal["spy_pct"] - 0.2) < 1e-9

    # Only HHHH's -15% slide clears the double-digit bar (CCCC -8% does not).
    assert report["avoided_slides"] == [{"ticker": "HHHH", "realized_pct": -15.0}]

    # Only window-closed tickers were fetched: the seven stale tickers plus SPY,
    # never FFFF (window open).
    assert "FFFF" not in FETCHED
    assert set(FETCHED) == {"AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "GGGG", "HHHH", "SPY"}
    print("grading + aggregates OK")

    # ---- outcomes persist; the second report re-fetches only the gap --------
    FETCHED.clear()
    report2 = await accuracy.accuracy_report()
    assert report2["graded"] == 6 and report2["pending"] == 2
    assert set(FETCHED) == {"GGGG", "SPY"}, FETCHED
    print("persistence OK")

    # ---- the API and the page ------------------------------------------------
    from app.main import app  # noqa: E402 - imported after env isolation

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        body = (await client.get("/api/accuracy")).json()
        assert body["graded"] == 6 and body["pending"] == 2
        assert body["horizon_days"] == settings.memory_horizon_days
        assert len(body["rows"]) == 6 and len(body["by_decision"]) == 3
        assert body["per_signal"]["n"] == 6 and len(body["avoided_slides"]) == 1
        assert body["rows"][0]["lesson"]

        page = await client.get("/accuracy")
        assert page.status_code == 200
        assert "Past calls vs the market" in page.text
    print("api + page OK")


asyncio.run(checks())
print("ACCURACY CHECKS PASSED")
