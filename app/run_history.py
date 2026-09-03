"""Durable SQLite storage for analysis run history."""

import asyncio
import json
import logging
import sqlite3
import time

from app.config import settings
from app.depth import DEFAULT_DEPTH, normalize_depth
from app.models import RunHistoryItem, RunStatus, StockAnalysis
from app.outlook import DEFAULT_OUTLOOK, normalize_outlook

logger = logging.getLogger("analysis")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db() -> None:
    now = time.time()
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_runs (
                run_id TEXT PRIMARY KEY,
                tickers_json TEXT NOT NULL,
                outlook TEXT NOT NULL DEFAULT 'short_term',
                depth TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL,
                mock_mode INTEGER NOT NULL DEFAULT 0,
                started_at REAL NOT NULL,
                completed_at REAL,
                duration_s REAL NOT NULL DEFAULT 0,
                error TEXT,
                results_json TEXT NOT NULL DEFAULT '{}',
                owner_id TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            )
            """
        )
        try:
            connection.execute(
                "ALTER TABLE analysis_runs ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            connection.execute(
                "ALTER TABLE analysis_runs ADD COLUMN outlook TEXT NOT NULL "
                f"DEFAULT '{DEFAULT_OUTLOOK}'"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            connection.execute(
                "ALTER TABLE analysis_runs ADD COLUMN depth TEXT NOT NULL "
                f"DEFAULT '{DEFAULT_DEPTH}'"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        connection.execute("DROP INDEX IF EXISTS idx_analysis_runs_started_at")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_analysis_runs_owner_started_at
            ON analysis_runs(owner_id, started_at DESC)
            """
        )
        # An in-memory workflow cannot resume after a process restart. Preserve
        # the record, but make its interrupted state honest and retryable.
        connection.execute(
            """
            UPDATE analysis_runs
            SET status = 'failed',
                completed_at = ?,
                duration_s = MAX(0, ? - started_at),
                error = COALESCE(error, 'Server restarted before analysis completed'),
                updated_at = ?
            WHERE status = 'running'
            """,
            (now, now, now),
        )
        connection.execute("PRAGMA optimize")


async def init() -> None:
    await asyncio.to_thread(_init_db)


def _save(status: RunStatus, completed_at: float | None, owner_id: str) -> None:
    results_json = json.dumps(
        {
            ticker: result.model_dump(mode="json")
            for ticker, result in status.results.items()
        },
        separators=(",", ":"),
    )
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO analysis_runs (
                run_id, tickers_json, outlook, depth, status, mock_mode, started_at,
                completed_at, duration_s, error, results_json, owner_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                tickers_json = excluded.tickers_json,
                outlook = excluded.outlook,
                depth = excluded.depth,
                status = excluded.status,
                mock_mode = excluded.mock_mode,
                completed_at = excluded.completed_at,
                duration_s = excluded.duration_s,
                error = excluded.error,
                results_json = excluded.results_json,
                owner_id = excluded.owner_id,
                updated_at = excluded.updated_at
            """,
            (
                status.run_id,
                json.dumps(status.tickers),
                normalize_outlook(status.outlook),
                normalize_depth(status.depth),
                status.status,
                int(status.mock_mode),
                status.started_at,
                completed_at,
                status.duration_s,
                status.error,
                results_json,
                owner_id,
                time.time(),
            ),
        )


async def save(
    status: RunStatus, completed_at: float | None = None, owner_id: str = ""
) -> None:
    await asyncio.to_thread(_save, status, completed_at, owner_id)


def _decode_status(row: sqlite3.Row) -> RunStatus:
    raw_results = json.loads(row["results_json"] or "{}")
    results = {
        ticker: StockAnalysis.model_validate(result)
        for ticker, result in raw_results.items()
    }
    return RunStatus(
        run_id=row["run_id"],
        tickers=json.loads(row["tickers_json"]),
        outlook=normalize_outlook(row["outlook"]),
        depth=normalize_depth(row["depth"]),
        status=row["status"],
        mock_mode=bool(row["mock_mode"]),
        started_at=row["started_at"],
        duration_s=row["duration_s"],
        error=row["error"],
        results=results,
    )


def _get(run_id: str) -> RunStatus | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None:
        return None
    try:
        return _decode_status(row)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[history] could not decode run %s: %s", run_id, exc)
        return None


async def get(run_id: str) -> RunStatus | None:
    return await asyncio.to_thread(_get, run_id)


def _list(limit: int, owner_id: str) -> list[RunHistoryItem]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM analysis_runs WHERE owner_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (owner_id, limit),
        ).fetchall()
    items: list[RunHistoryItem] = []
    for row in rows:
        try:
            status = _decode_status(row)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("[history] skipping corrupt run %s: %s", row["run_id"], exc)
            continue
        items.append(
            RunHistoryItem(
                run_id=status.run_id,
                tickers=status.tickers,
                outlook=status.outlook,
                depth=status.depth,
                status=status.status,
                mock_mode=status.mock_mode,
                started_at=status.started_at,
                duration_s=status.duration_s,
                error=status.error,
                result_count=len(status.results),
                has_errors=any(result.error for result in status.results.values()),
                decisions={
                    ticker: result.decision
                    for ticker, result in status.results.items()
                    if not result.error
                },
            )
        )
    return items


async def list_runs(owner_id: str, limit: int = 50) -> list[RunHistoryItem]:
    return await asyncio.to_thread(_list, min(100, max(1, limit)), owner_id)


def _clear(owner_id: str) -> list[str]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT run_id FROM analysis_runs "
            "WHERE owner_id = ? AND status != 'running'",
            (owner_id,),
        ).fetchall()
        run_ids = [str(row["run_id"]) for row in rows]
        connection.execute(
            "DELETE FROM analysis_runs WHERE owner_id = ? AND status != 'running'",
            (owner_id,),
        )
    return run_ids


async def clear(owner_id: str) -> list[str]:
    return await asyncio.to_thread(_clear, owner_id)
