"""Offline checks for run controls: cancel, rerun, partial results (ROADMAP P0.2).

Run: uv run python -m scripts.check_run_controls
"""

import asyncio
import os
from pathlib import Path
import sqlite3
import tempfile

history_db = Path(tempfile.mkdtemp()) / "run_controls_test.db"
os.environ["DB_PATH"] = str(history_db)

import httpx  # noqa: E402

from app import memory, portfolio, run_history, runs  # noqa: E402
from app.main import app  # noqa: E402
from app.models import StockAnalysis  # noqa: E402

# ticker -> (data-fetch seconds, agent-work seconds). AAA finishes fast; when
# the cancel lands, BBB is mid-agent-work and CCC is mid-data-fetch.
PHASES = {"AAA": (0.05, 0.05), "BBB": (0.15, 30.0), "CCC": (30.0, 30.0)}
portfolio_delay = {"seconds": 0.0}

original_analyze = runs.analyze_ticker
original_portfolio = runs.fetch_portfolio_summary


async def fake_analyze(ticker: str, emit, run_id: str = "", **_kwargs) -> StockAnalysis:
    data_s, agent_s = PHASES.get(ticker, (0.05, 0.05))
    await emit("ticker_started", {"ticker": ticker})
    await asyncio.sleep(data_s)
    await emit(
        "ticker_data",
        {
            "ticker": ticker,
            "price": 100.0,
            "company_name": "Test Corp",
            "sources": {"prices": "test"},
        },
    )
    await asyncio.sleep(agent_s)
    analysis = StockAnalysis(
        ticker=ticker, company_name="Test Corp", price=100.0,
        decision="HOLD", confidence=0.5,
    )
    # Mirror the real workflow: a decision is recorded only for a ticker whose
    # analysis ran all the way to completion, and its result is announced.
    await memory.record_decision(run_id, analysis)
    await emit(
        "ticker_completed",
        {
            "ticker": ticker,
            "decision": analysis.decision,
            "confidence": analysis.confidence,
            "duration_s": data_s + agent_s,
            "analysis": analysis.model_dump(),
        },
    )
    return analysis


async def fake_portfolio_summary():
    await asyncio.sleep(portfolio_delay["seconds"])
    return None


async def checks() -> None:
    await portfolio.init()
    await run_history.init()
    await memory.init()
    runs.analyze_ticker = fake_analyze
    runs.fetch_portfolio_summary = fake_portfolio_summary
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            assert (await client.post("/api/runs/nosuchrun00/cancel")).status_code == 404

            # Cancel mid-run: AAA finished, BBB mid-agent-work, CCC mid-data-fetch.
            run = await runs.store.create(
                ["AAA", "BBB", "CCC"], client_id="device_run_controls"
            )
            await asyncio.sleep(0.5)
            cancelled = await client.post(f"/api/runs/{run.run_id}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert run.status == "cancelled"  # terminal before the pipeline settles
            await run.execution_task

            assert run.status == "cancelled", "a cancelled run cannot flip to completed"
            assert set(run.results) == {"AAA"}, "finished tickers are kept, interrupted ones dropped"
            terminal = [e for e in run.events if e["type"] == "analysis_completed"]
            assert len(terminal) == 1 and terminal[0]["status"] == "cancelled"
            data_seen = {e["ticker"] for e in run.events if e["type"] == "ticker_data"}
            assert data_seen == {"AAA", "BBB"}, "CCC was still in its data fetch"
            completed_seen = {
                e["ticker"] for e in run.events if e["type"] == "ticker_completed"
            }
            assert completed_seen == {"AAA"}
            assert not runs.store.analysis_inflight and not runs.store.analysis_waiters

            # Repeated cancels are harmless no-ops reporting the same status.
            repeat = await client.post(f"/api/runs/{run.run_id}/cancel")
            assert repeat.status_code == 200 and repeat.json()["status"] == "cancelled"
            await asyncio.sleep(0.1)
            assert run.status == "cancelled"

            # Refresh and restart paths restore the same partial results.
            detail = await client.get(f"/api/runs/{run.run_id}")
            assert detail.status_code == 200 and detail.json()["status"] == "cancelled"
            assert set(detail.json()["results"]) == {"AAA"}
            persisted = await run_history.get(run.run_id)
            assert persisted is not None and persisted.status == "cancelled"
            assert set(persisted.results) == {"AAA"}
            history = await client.get(
                "/api/runs", headers={"X-Client-ID": "device_run_controls"}
            )
            assert history.json()[0]["status"] == "cancelled"
            assert history.json()[0]["result_count"] == 1

            # Only the ticker that finished recorded a decision.
            with sqlite3.connect(history_db) as connection:
                recorded = {
                    row[0] for row in connection.execute("SELECT ticker FROM decisions")
                }
            assert recorded == {"AAA"}, "interrupted tickers must not record decisions"

            # Rerun with the same settings works after a cancel.
            rerun = await client.post(
                "/api/analyze",
                json={
                    "tickers": ["AAA"],
                    "outlook": "short_term",
                    "depth": "medium",
                    "client_id": "device_run_controls",
                },
            )
            assert rerun.status_code == 200
            new_run = runs.store.get(rerun.json()["run_id"])
            assert new_run is not None and new_run is not run
            await new_run.execution_task
            assert new_run.status == "completed" and set(new_run.results) == {"AAA"}

            # Cancel while the shared portfolio snapshot is still loading:
            # no ticker task exists yet, and the run must still terminate once.
            portfolio_delay["seconds"] = 30.0
            early = await runs.store.create(["AAA"], client_id="device_run_controls")
            await asyncio.sleep(0.2)
            early_cancel = await client.post(f"/api/runs/{early.run_id}/cancel")
            assert early_cancel.status_code == 200
            assert early_cancel.json()["status"] == "cancelled"
            await early.execution_task
            assert early.status == "cancelled"
            assert not early.ticker_tasks, "no analysis should start after cancellation"
            assert not any(e["type"] == "ticker_started" for e in early.events)
            early_terminal = [
                e for e in early.events if e["type"] == "analysis_completed"
            ]
            assert len(early_terminal) == 1 and early_terminal[0]["status"] == "cancelled"
    finally:
        runs.analyze_ticker = original_analyze
        runs.fetch_portfolio_summary = original_portfolio


asyncio.run(checks())
print("RUN CONTROLS CHECKS PASSED")
