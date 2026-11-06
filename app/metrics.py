"""Operational metrics (wiki P2.2): one read-only snapshot of process,
database, trading, and automation state for GET /api/metrics.

Everything comes from the local database and process itself - no provider
calls, no locks beyond the one connection - so the endpoint is safe to poll.
Shape is a plain dict on purpose: ops surfaces grow row by row, and a typed
model would make every addition a breaking change for consumers.
"""

from __future__ import annotations

import os
import platform
import time
from datetime import datetime, timezone

from app import automation
from app.config import settings
from app.db import connect as _connect
from app.migrations import LATEST

_started_at: float | None = None


def mark_started() -> None:
    """Called once at startup; uptime counts from here, not from import."""
    global _started_at
    if _started_at is None:
        _started_at = time.time()


def _count(connection, sql: str, params: tuple = ()) -> int:
    return int(connection.execute(sql, params).fetchone()[0])


def snapshot() -> dict:
    from app import backup

    now = time.time()
    day_ago = now - 86400.0
    backups = backup.list_backups()
    with _connect() as connection:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        orders_by_status = {
            row["status"]: row["n"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS n FROM broker_orders GROUP BY status"
            ).fetchall()
        }
        sync_row = connection.execute(
            "SELECT checked_at, in_sync, reconciled, error FROM broker_sync WHERE id = 1"
        ).fetchone()
        hwm_row = connection.execute(
            "SELECT high_water, marked_at FROM equity_hwm WHERE id = 1"
        ).fetchone()
        automation_enabled = bool(
            (
                connection.execute(
                    "SELECT enabled FROM automation_state WHERE id = 1"
                ).fetchone()
                or {"enabled": 0}
            )["enabled"]
        )
        payload = {
            "app": {
                "version": _version(),
                "python": platform.python_version(),
                "platform": platform.system(),
                "started_at": _started_at,
                "uptime_s": max(0.0, now - _started_at) if _started_at else None,
            },
            "database": {
                "path": str(settings.db_path),
                "size_bytes": _db_size(),
                "schema_version": schema_version,
                "schema_latest": LATEST,
            },
            "runs": {
                "total": _count(connection, "SELECT COUNT(*) FROM analysis_runs"),
                "last_24h": _count(
                    connection,
                    "SELECT COUNT(*) FROM analysis_runs WHERE started_at >= ?",
                    (day_ago,),
                ),
                "failed_last_24h": _count(
                    connection,
                    "SELECT COUNT(*) FROM analysis_runs "
                    "WHERE started_at >= ? AND status = 'failed'",
                    (day_ago,),
                ),
            },
            "orders": {
                "total": sum(orders_by_status.values()),
                "by_status": orders_by_status,
            },
            "decisions": {
                "total": _count(connection, "SELECT COUNT(*) FROM decisions"),
            },
            "automation": {
                "enabled": automation_enabled,
                "session_active": automation._session_lock.locked(),
                "sessions_last_24h": _count(
                    connection,
                    "SELECT COUNT(*) FROM automation_sessions WHERE started_at >= ?",
                    (day_ago,),
                ),
                "exit_attempts_today": _count(
                    connection,
                    "SELECT COUNT(*) FROM automation_exit_attempts WHERE day = ?",
                    (datetime.now(timezone.utc).strftime("%Y-%m-%d"),),
                ),
            },
            "broker": {
                "trading_enabled": settings.alpaca_trading_enabled,
                "in_sync": bool(sync_row["in_sync"]) if sync_row else None,
                "last_sync_at": sync_row["checked_at"] if sync_row else None,
                "equity_high_water": hwm_row["high_water"] if hwm_row else None,
            },
            "backups": {
                "dir": str(backup.backup_dir()),
                "count": len(backups),
                "latest": backups[0] if backups else None,
            },
        }
    return payload


def _db_size() -> int | None:
    try:
        return os.path.getsize(settings.db_path)
    except OSError:
        return None


def _version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version

    try:
        return dist_version("better-trading-agents")
    except PackageNotFoundError:
        return "unknown"
