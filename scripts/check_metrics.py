"""Operational metrics checks: the /api/metrics snapshot (P2.2).

Counts must reflect real rows, the schema version must match the migration
head, and the API must serve the same payload offline.

Run: uv run test metrics
"""

import os
import tempfile
from pathlib import Path

# Isolated DB and cleared env before app.config is imported.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "metrics_test.db")
os.environ["BACKUP_DIR"] = str(Path(tempfile.mkdtemp()) / "backups")
os.environ["LLM_API_KEY"] = ""
os.environ["ALPACA_API_KEY"] = ""
os.environ["ALPACA_SECRET_KEY"] = ""

import httpx  # noqa: E402

from app import automation, backup, broker, memory, metrics, migrations, run_history  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models import RunStatus, StockAnalysis  # noqa: E402


async def main() -> None:
    await memory.init()
    await run_history.init()
    await broker.init()
    await automation.init()
    migrations.apply_latest()
    metrics.mark_started()

    snap = metrics.snapshot()
    assert snap["app"]["uptime_s"] is not None and snap["app"]["uptime_s"] >= 0
    assert snap["app"]["version"] not in ("", None)
    assert snap["database"]["schema_version"] == migrations.LATEST
    assert snap["database"]["size_bytes"] and snap["database"]["size_bytes"] > 0
    assert snap["runs"]["total"] == 0 and snap["orders"]["total"] == 0
    assert snap["automation"]["enabled"] is False
    assert snap["backups"]["count"] == 0
    print("empty-database snapshot OK")

    # Real rows move the counters.
    await run_history.save(
        RunStatus(
            run_id="metricsrun1",
            tickers=["NVDA"],
            status="completed",
            started_at=0.0,
            duration_s=1.0,
            results={
                "NVDA": StockAnalysis(ticker="NVDA", price=1.0, decision="HOLD",
                                      confidence=0.5, as_of=""),
            },
        ),
        owner_id="metrics_check",
    )
    broker._insert_order(
        "bta-metrics-1", "metricsrun1", "NVDA", "buy", 1.0, 100.0, "BUY", 0.9, ""
    )
    backup.create_backup()
    snap = metrics.snapshot()
    assert snap["runs"]["total"] == 1 and snap["runs"]["last_24h"] == 0, snap["runs"]
    # started_at=0.0 is 55 years ago: last_24h stays 0, total moves. The order
    # and backup counters must both see their rows.
    assert snap["orders"]["by_status"].get("pending_submit") == 1
    assert snap["backups"]["count"] == 1 and snap["backups"]["latest"]["size_bytes"] > 0
    print("row-counting snapshot OK (total vs last_24h, orders, backups)")

    # The API serves the same payload.
    settings.owner_token = ""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        response = await c.get("/api/metrics")
        assert response.status_code == 200
        payload = response.json()
    assert payload["runs"]["total"] == snap["runs"]["total"]
    assert payload["database"]["schema_version"] == migrations.LATEST
    print("GET /api/metrics OK")

    print("ALL METRICS CHECKS PASSED")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
