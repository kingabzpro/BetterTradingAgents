"""Offline checks for search, filters, and portable reports (ROADMAP P1.6).

Run: PYTHONPATH=. uv run python scripts/check_search_export.py
No network: run history is written directly to SQLite and pages are served
through the ASGI transport. The behavioral part (instant filtering, download
contents, keyboard, 320 px) lives in scripts/check_browser_smoke.py; here we
pin the page structure, the print stylesheet, and the JSON export payload.
"""

import asyncio
import os
from pathlib import Path
import tempfile

# Isolated DB before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "search_export_test.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""

import httpx  # noqa: E402

from app import run_history  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AgentResult, DataQuality, StockAnalysis  # noqa: E402

OWNER = "device_search_export_test"


def _analysis(ticker: str, decision: str, confidence: float) -> StockAnalysis:
    return StockAnalysis(
        ticker=ticker,
        company_name=f"{ticker} Inc.",
        price=100.0,
        decision=decision,  # type: ignore[arg-type]
        confidence=confidence,
        as_of="2026-09-20T12:00:00+00:00",
        data_quality=DataQuality(age_hours=2.0),
        technical=AgentResult(agent="technical", signal="bullish"),
    )


async def checks() -> None:
    await run_history.init()

    # Two runs with different shapes so the /api/runs payload the client
    # filters has every dimension the filter bar exposes.
    from app.models import RunStatus

    run_a = RunStatus(
        run_id="srchrun000001",
        tickers=["AAPL"],
        outlook="short_term",
        depth="medium",
        status="completed",
        started_at=1_780_000_000.0,
        duration_s=10.0,
        results={"AAPL": _analysis("AAPL", "BUY", 0.72)},
    )
    run_b = RunStatus(
        run_id="srchrun000002",
        tickers=["MSFT"],
        outlook="long_term",
        depth="fast",
        status="cancelled",
        started_at=1_780_100_000.0,
        duration_s=3.0,
        results={},
    )
    await run_history.save(run_a, completed_at=1_780_000_010.0, owner_id=OWNER)
    await run_history.save(run_b, completed_at=1_780_100_011.0, owner_id=OWNER)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ---- runs page: search, every filter, sort, chips, empty states ----
        page = await client.get("/history")
        assert page.status_code == 200, page.text
        text = page.text
        for marker in (
            'id="run-search"',
            'id="filter-status"',
            'id="filter-outlook"',
            'id="filter-depth"',
            'id="filter-decision"',
            'id="filter-from"',
            'id="filter-to"',
            'id="run-sort"',
            'id="filter-chips"',
            'id="filter-count"',
            'id="history-no-match"',
            ">Clear filters<",
            ">Filters<",
        ):
            assert marker in text, f"runs page missing: {marker}"
        print("runs page filter bar OK")

        # ---- portfolio page: the paper view and its CSV export ----
        page = await client.get("/portfolio")
        assert page.status_code == 200, page.text
        text = page.text
        for marker in (
            'id="paper-connect-hint"',
            'id="paper-takeover"',
            'id="paper-positions-table"',
            'id="orders-table"',
            'id="perf-table"',
            'id="download-csv"',
            "Download CSV",
        ):
            assert marker in text, f"portfolio page missing: {marker}"
        print("portfolio page paper view OK")

        # ---- decision-brief data the client renders is in the run payload ----
        detail = await client.get(f"/api/runs/{run_a.run_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["run_id"] == run_a.run_id
        assert body["results"]["AAPL"]["decision"] == "BUY"
        assert detail.headers["content-type"].startswith("application/json")
        print("run JSON export payload OK")

        missing = await client.get("/api/runs/does-not-exist")
        assert missing.status_code == 404

        # ---- print stylesheet: grayscale-safe brief scoping ----
        css = await client.get("/static/css/analysis.css")
        assert css.status_code == 200
        css_text = css.text
        for marker in (
            "@media print",
            "printing-brief",
            "print-stamp",
            ".result-detail { display: block !important; }",
        ):
            assert marker in css_text, f"print CSS missing: {marker}"
        home = await client.get("/")
        assert home.status_code == 200
        assert "/static/css/analysis.css?v=4" in home.text, "analysis page must load the print CSS"
        print("print stylesheet OK")

        # ---- JS assets ship the client-side logic ----
        for asset, marker in (
            ("/static/js/history.js", "visibleRuns"),
            ("/static/js/history.js", "Download JSON"),
            ("/static/js/history.js", "downloadFile"),
            ("/static/js/portfolio.js", "downloadCsv"),
            ("/static/js/render.js", "printBrief"),
        ):
            script = await client.get(asset)
            assert script.status_code == 200
            assert marker in script.text, f"{asset} missing: {marker}"
        print("client-side filter and export code OK")


asyncio.run(checks())
print("SEARCH EXPORT CHECKS PASSED")
