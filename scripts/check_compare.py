"""Offline checks for decision comparison and `What changed` (ROADMAP P1.5).

Run: PYTHONPATH=. uv run python scripts/check_compare.py
No network: prices are stubbed and run history is written directly to SQLite.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile

# Isolated DB before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "compare_test.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""

import httpx  # noqa: E402

from app import changes, run_history  # noqa: E402
from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AgentResult,
    DataQuality,
    RunStatus,
    StockAnalysis,
)

OWNER = "device_compare_test"


def _analysis(
    decision: str,
    confidence: float,
    price: float = 100.0,
    forecast: float | None = 105.0,
    signals: dict[str, str] | None = None,
    flags: list[str] | None = None,
    age: float | None = 2.0,
    stale: bool = False,
    as_of: str = "2026-09-20T12:00:00+00:00",
) -> StockAnalysis:
    signals = signals or {"technical": "bullish", "news": "neutral"}
    return StockAnalysis(
        ticker="AAPL",
        company_name="Apple Inc.",
        price=price,
        forecast_price_5d=forecast,
        decision=decision,  # type: ignore[arg-type]
        confidence=confidence,
        as_of=as_of,
        risk_flags=flags or [],
        data_quality=DataQuality(age_hours=age, stale=stale),
        **{
            key: AgentResult(agent=key, signal=signal)
            for key, signal in signals.items()
        },
    )


def _run(run_id: str, started_at: float, analysis: StockAnalysis) -> RunStatus:
    return RunStatus(
        run_id=run_id,
        tickers=[analysis.ticker],
        outlook="short_term",
        depth="medium",
        status="completed",
        started_at=started_at,
        duration_s=10.0,
        results={analysis.ticker: analysis},
    )


async def checks() -> None:
    await run_history.init()

    # ---- what_changed: every compared dimension, changed and unchanged ------
    base = _analysis("BUY", 0.72)
    same = _analysis("BUY", 0.72)
    prev = changes.snapshot(base)
    # The run lookup fills analyzed_at; set it so the freshness diff has both sides.
    prev.analyzed_at = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc).timestamp()
    assert changes.what_changed(same, prev) == [
        "Decision unchanged (BUY)",
        "Evidence strength unchanged (strong, 72%)",
        "Analyst signals unchanged (1 bullish, 1 neutral)",
        "5-day forecast unchanged ($105.00)",
        "Data freshness: 2.0h old -> 2.0h old",
        "Risk flags unchanged (none)",
    ]
    # Deterministic: the same inputs must always give the same lines.
    assert changes.what_changed(same, prev) == changes.what_changed(same, prev)
    print("unchanged case OK")

    moved = _analysis(
        "SELL",
        0.55,
        forecast=90.0,
        signals={"technical": "bearish", "news": "neutral", "sentiment": "bearish"},
        flags=["position size capped"],
        age=30.0,
        stale=True,
    )
    lines = changes.what_changed(moved, prev)
    assert lines[0] == "Decision: BUY -> SELL"
    assert lines[1] == "Evidence strength: strong 72% -> moderate 55%"
    assert lines[2] == "Analyst signals: technical bullish -> bearish; sentiment not run -> bearish"
    assert lines[3] == "5-day forecast: $105.00 -> $90.00"
    assert lines[4] == "Data freshness: 2.0h old -> 30.0h old (stale)"
    assert lines[5] == "Risk flags: added position size capped"
    print("changed case OK:", lines[0])

    cleared = _analysis("BUY", 0.72, flags=[])
    prev_flags = changes.snapshot(_analysis("BUY", 0.72, flags=["low liquidity"]))
    assert changes.what_changed(cleared, prev_flags)[5] == "Risk flags: cleared low liquidity"
    print("risk flag diff OK")

    # ---- attach sets and clears the comparison pair --------------------------
    analysis = _analysis("BUY", 0.72)
    changes.attach(analysis, prev)
    assert analysis.previous is prev and len(analysis.what_changed) == 6
    changes.attach(analysis, None)
    assert analysis.previous is None and analysis.what_changed == []
    print("attach OK")

    # ---- previous_call lookup ----------------------------------------------
    run1 = _run("comprun000001", 1_780_000_000.0, _analysis("BUY", 0.72, price=190.0))
    await run_history.save(run1, completed_at=1_780_000_010.0, owner_id=OWNER)
    run2 = _run("comprun000002", 1_780_100_000.0, _analysis("SELL", 0.40, price=175.0))
    await run_history.save(run2, completed_at=1_780_100_011.0, owner_id=OWNER)

    # A third run in progress picks the newest completed call before its start.
    call = await changes.previous_call(OWNER, "AAPL", "comprun000003", 1_780_200_000.0)
    assert call is not None and call.run_id == "comprun000002"
    assert call.decision == "SELL" and call.analyzed_at == 1_780_100_000.0
    assert call.signals.get("technical") == "bullish"

    # The run itself is never its own previous; the older call wins then.
    call = await changes.previous_call(OWNER, "AAPL", "comprun000002", 1_780_100_000.0)
    assert call is not None and call.run_id == "comprun000001" and call.decision == "BUY"

    # Nothing before the first call, nothing for an unanalyzed ticker.
    assert await changes.previous_call(OWNER, "AAPL", "comprun000001", 1_780_000_000.0) is None
    assert await changes.previous_call(OWNER, "MSFT", "comprun000003", 1_780_200_000.0) is None

    # Failed results are skipped: a later failed run is not a previous call.
    failed = _run("comprun000004", 1_780_300_000.0, _analysis("BUY", 0.5))
    failed.results["AAPL"].error = "boom"
    await run_history.save(failed, completed_at=1_780_300_001.0, owner_id=OWNER)
    call = await changes.previous_call(OWNER, "AAPL", "comprun000005", 1_780_400_000.0)
    assert call is not None and call.run_id == "comprun000002"
    print("previous_call OK")

    # ---- price-history endpoint --------------------------------------------
    async def fake_closes(ticker: str, start: str, end: str):
        assert ticker == "AAPL" and start < end
        return {"2026-04-01": 180.0, "2026-04-02": 182.5, "2026-04-03": 181.0}

    main_module.get_closes_between = fake_closes

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        history = await client.get("/api/price-history/AAPL")
        assert history.status_code == 200, history.text
        body = history.json()
        assert body["ticker"] == "AAPL"
        assert body["dates"] == ["2026-04-01", "2026-04-02", "2026-04-03"]
        assert body["closes"] == [180.0, 182.5, 181.0]

        bad = await client.get("/api/price-history/not a ticker")
        assert bad.status_code == 400

        # An empty provider history must come back empty, never invented points.
        async def no_closes(ticker: str, start: str, end: str):
            return {}

        main_module.get_closes_between = no_closes
        empty = await client.get("/api/price-history/AAPL")
        assert empty.status_code == 200
        assert empty.json() == {"ticker": "AAPL", "dates": [], "closes": []}

        # ---- compare page ----------------------------------------------------
        page = await client.get("/compare")
        assert page.status_code == 200
        assert "Compare decisions" in page.text
        assert "Add to compare" in page.text
        assert "What changed" in page.text
    print("price-history + compare page OK")


asyncio.run(checks())
print("COMPARE CHECKS PASSED")
