"""Durable SQLite storage for analysis run history."""

import asyncio
import json
import logging
import sqlite3
import time
from typing import NamedTuple

from app.depth import DEFAULT_DEPTH, normalize_depth
from app.models import RunHistoryItem, RunStatus, StockAnalysis
from app.outlook import DEFAULT_OUTLOOK, normalize_outlook
from app.db import connect as _connect

logger = logging.getLogger("analysis")


class NewestAnalysis(NamedTuple):
    """One ticker's newest completed result, with the run that produced it."""

    run_id: str
    started_at: float
    analysis: StockAnalysis




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


def _owner(run_id: str) -> str | None:
    """Just the owning client id, without decoding the run's payload."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT owner_id FROM analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    return row["owner_id"] if row else None


async def owner_of(run_id: str) -> str | None:
    return await asyncio.to_thread(_owner, run_id)


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
            raw_results = json.loads(row["results_json"] or "{}")
            tickers = json.loads(row["tickers_json"])
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("[history] skipping corrupt run %s: %s", row["run_id"], exc)
            continue
        if not isinstance(raw_results, dict):
            logger.warning("[history] skipping corrupt run %s", row["run_id"])
            continue
        # The listing only needs decision/error/cost/tokens, so read those
        # straight from the JSON instead of Pydantic-decoding every full
        # agent payload of every run on every page load (P2.10). Only the
        # detail view needs the full decode.
        any_tokens = False
        cost_usd = 0.0
        cost_unknown = False
        has_errors = False
        decisions: dict[str, str | None] = {}
        for ticker, result in raw_results.items():
            if result.get("error"):
                has_errors = True
                continue
            decisions[ticker] = result.get("decision")
            estimate = result.get("cost_estimate") or {}
            if result.get("token_usage"):
                any_tokens = True
                if not estimate or not estimate.get("priced"):
                    cost_unknown = True
            cost_usd += float(estimate.get("total_usd") or 0.0)
        items.append(
            RunHistoryItem(
                run_id=row["run_id"],
                tickers=tickers,
                outlook=normalize_outlook(row["outlook"]),
                depth=normalize_depth(row["depth"]),
                status=row["status"],
                mock_mode=bool(row["mock_mode"]),
                started_at=row["started_at"],
                duration_s=row["duration_s"],
                error=row["error"],
                result_count=len(raw_results),
                has_errors=has_errors,
                decisions=decisions,
                cost_usd=round(cost_usd, 6) if any_tokens else None,
                cost_unknown=cost_unknown,
            )
        )
    return items


async def list_runs(owner_id: str, limit: int = 50) -> list[RunHistoryItem]:
    return await asyncio.to_thread(_list, min(100, max(1, limit)), owner_id)


def _newest_analysis(
    owner_id: str,
    ticker: str,
    exclude_run_id: str,
    before_at: float | None,
    after_at: float | None,
) -> NewestAnalysis | None:
    """Newest completed, non-error result for one ticker, in one SQL probe.

    The JSON1 match pushes the ticker and error filters into SQLite so at
    most one row is ever decoded, where the listing round-trip used to
    Pydantic-decode up to 100 full runs per lookup (P2.10). Shared by
    What-changed and the watchlist.
    """
    sql = (
        "SELECT run_id, started_at, results_json FROM analysis_runs "
        "WHERE owner_id = ? AND status = 'completed' AND run_id != ? "
        "AND json_valid(results_json) "
        "AND json_extract(results_json, ?) IS NOT NULL "
        "AND json_extract(results_json, ?) IS NULL "
    )
    params: list = [
        owner_id,
        exclude_run_id or "",
        f'$."{ticker}"',
        f'$."{ticker}".error',
    ]
    if before_at is not None:
        sql += "AND started_at < ? "
        params.append(before_at)
    if after_at is not None:
        sql += "AND started_at > ? "
        params.append(after_at)
    sql += "ORDER BY started_at DESC LIMIT 1"
    with _connect() as connection:
        row = connection.execute(sql, params).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row["results_json"] or "{}")[ticker]
        return NewestAnalysis(
            run_id=row["run_id"],
            started_at=row["started_at"],
            analysis=StockAnalysis.model_validate(raw),
        )
    except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        logger.warning("[history] could not decode newest %s result: %s", ticker, exc)
        return None


async def newest_analysis(
    owner_id: str,
    ticker: str,
    exclude_run_id: str = "",
    before_at: float | None = None,
    after_at: float | None = None,
) -> NewestAnalysis | None:
    return await asyncio.to_thread(
        _newest_analysis, owner_id, ticker, exclude_run_id, before_at, after_at
    )


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
