"""Offline integration checks for durable analysis run history."""

import asyncio
import os
from pathlib import Path
import sqlite3
import tempfile

history_db = Path(tempfile.mkdtemp()) / "run_history_test.db"
os.environ["DB_PATH"] = str(history_db)

import httpx  # noqa: E402

from app import portfolio, run_history  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AgentResult, RunStatus, SourceReference, StockAnalysis  # noqa: E402


async def checks() -> None:
    await portfolio.init()
    await run_history.init()
    analysis = StockAnalysis(
        ticker="TSLA",
        company_name="Tesla, Inc.",
        price=351.95,
        decision="HOLD",
        confidence=0.61,
        as_of="2026-09-02T10:00:00+00:00",
        providers={"prices": "yfinance", "news": "finnhub"},
        source_references=[
            SourceReference(
                kind="news",
                title="Example headline",
                provider="Example News",
                url="https://example.com/story",
            )
        ],
        bull_rebuttal=AgentResult(
            agent="bull", signal="bullish", confidence=0.65, summary="Rebuttal"
        ),
    )
    completed = RunStatus(
        run_id="completed001",
        tickers=["TSLA"],
        status="completed",
        started_at=1_780_000_000,
        duration_s=14.2,
        results={"TSLA": analysis},
    )
    await run_history.save(
        completed, completed_at=1_780_000_014.2, owner_id="device_history_test"
    )

    restored = await run_history.get("completed001")
    assert restored is not None
    assert restored.results["TSLA"].source_references[0].url == "https://example.com/story"
    assert restored.results["TSLA"].bull_rebuttal is not None

    running = RunStatus(
        run_id="interrupted1",
        tickers=["NVDA", "AMD"],
        status="running",
        started_at=1_780_000_100,
    )
    await run_history.save(running, owner_id="device_history_test")
    await run_history.init()
    interrupted = await run_history.get("interrupted1")
    assert interrupted is not None and interrupted.status == "failed"
    assert "restarted" in (interrupted.error or "").lower()

    active = RunStatus(
        run_id="active000001",
        tickers=["META"],
        status="running",
        started_at=1_780_000_200,
    )
    await run_history.save(active, owner_id="device_history_test")

    items = await run_history.list_runs("device_history_test", 50)
    assert [item.run_id for item in items] == [
        "active000001",
        "interrupted1",
        "completed001",
    ]
    assert items[2].decisions == {"TSLA": "HOLD"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get(
            "/api/runs", headers={"X-Client-ID": "device_history_test"}
        )
        assert listing.status_code == 200 and len(listing.json()) == 3
        private_listing = await client.get(
            "/api/runs", headers={"X-Client-ID": "different_device"}
        )
        assert private_listing.status_code == 200 and private_listing.json() == []
        assert (await client.get("/api/runs")).json() == []
        detail = await client.get("/api/runs/completed001")
        assert detail.status_code == 200
        assert detail.json()["results"]["TSLA"]["company_name"] == "Tesla, Inc."
        page = await client.get("/history")
        assert page.status_code == 200 and "Analysis runs" in page.text
        cleared = await client.delete(
            "/api/runs", headers={"X-Client-ID": "device_history_test"}
        )
        assert cleared.status_code == 200 and cleared.json()["deleted"] == 2
        remaining = await client.get(
            "/api/runs", headers={"X-Client-ID": "device_history_test"}
        )
        assert [item["run_id"] for item in remaining.json()] == ["active000001"]
        assert (await client.get("/api/runs/completed001")).status_code == 404

    with sqlite3.connect(history_db) as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index'"
            )
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert {"positions", "analysis_runs"}.issubset(tables)
        assert "idx_analysis_runs_owner_started_at" in indexes
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM analysis_runs WHERE owner_id = ? "
            "ORDER BY started_at DESC LIMIT 50",
            ("device_history_test",),
        ).fetchall()
        assert any("idx_analysis_runs_owner_started_at" in str(row) for row in plan)


asyncio.run(checks())
print("RUN HISTORY CHECKS PASSED")
