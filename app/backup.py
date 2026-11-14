"""Scheduled backups of the portfolio database (wiki P2.2).

SQLite's online backup API copies the live database - WAL and all - into a
timestamped file, so every snapshot is consistent even while runs, orders,
and sessions are being written; the copy never blocks the writers for more
than SQLite needs to hand over pages. One snapshot runs at startup, the
background loop adds one every BACKUP_INTERVAL_HOURS (0 = off, the loop
rechecks the setting each round), and POST /api/backups runs one on demand.
Retention keeps the newest BACKUP_KEEP files named portfolio-*.db and never
touches anything else in the directory.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.db import connect as _connect

logger = logging.getLogger("backup")

# The naming convention is this module's namespace: only files that match it
# are ever listed or pruned, so a user's other files in the folder are safe.
_FILE_RE = re.compile(r"^portfolio-\d{8}-\d{6}-\d{3}\.db$")

_backup_task: asyncio.Task | None = None


def backup_dir() -> Path:
    """Where snapshots go: the configured folder, else backups/ beside the
    database itself, so the copies travel with the data they protect."""
    if settings.backup_dir:
        return Path(settings.backup_dir)
    return Path(settings.db_path).parent / "backups"


def _describe(path: Path) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "size_bytes": stat.st_size,
        "created_at": stat.st_mtime,
    }


def create_backup() -> dict:
    """One consistent snapshot; returns the record the API and metrics show."""
    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # Milliseconds in the name: a startup backup and a manual one can land in
    # the same second, and one must never overwrite the other.
    now = datetime.now(timezone.utc)
    target = directory / f"portfolio-{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}.db"
    with _connect() as source:
        destination = sqlite3.connect(target)
        try:
            with destination:
                source.backup(destination)
        finally:
            destination.close()
    _prune()
    logger.info("[backup] wrote %s", target)
    return _describe(target)


def _prune() -> None:
    keep = settings.backup_keep
    directory = backup_dir()
    files = [p for p in directory.iterdir() if p.is_file() and _FILE_RE.match(p.name)]
    # Zero-padded timestamps make name order the age order.
    for path in sorted(files, key=lambda p: p.name)[: max(0, len(files) - keep)]:
        # Sidecars of a removed snapshot are orphans; take them with it.
        for victim in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            try:
                victim.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("[backup] could not remove old backup %s: %s", victim, exc)


def list_backups() -> list[dict]:
    directory = backup_dir()
    if not directory.exists():
        return []
    files = [p for p in directory.iterdir() if p.is_file() and _FILE_RE.match(p.name)]
    return [_describe(p) for p in sorted(files, key=lambda p: p.name, reverse=True)]


async def run_backup() -> dict:
    return await asyncio.to_thread(create_backup)


async def _loop() -> None:
    while True:
        interval_hours = settings.backup_interval_hours
        if interval_hours <= 0:
            await asyncio.sleep(300)  # off; recheck live instead of dying
            continue
        await asyncio.sleep(interval_hours * 3600.0)
        try:
            await run_backup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop outlives any bug
            logger.error("[backup] scheduled backup failed: %s", exc)


def start() -> None:
    """Spawn the backup loop (idempotent; called from app startup)."""
    global _backup_task
    if _backup_task is None or _backup_task.done():
        _backup_task = asyncio.create_task(_loop(), name="backup-loop")
