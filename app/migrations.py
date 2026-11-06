"""Versioned SQLite schema migrations with tested rollback (wiki P2.2).

Each module still owns its tables' initial CREATE (the IF NOT EXISTS in its
_init_db), so a fresh database needs no migration to boot. This module owns
every shape change to an existing table after first release: one numbered
step, an up that applies it, and a down that reverses it, so an upgrade is
reviewable and a rollback is a real, tested path instead of a restore from
hope. The version counter is PRAGMA user_version on the portfolio database
itself - no extra table, and it travels with the file.

A step must leave the app working whether it ran or not (the module inits
stay idempotent), and a down must be the honest inverse: schema back to the
previous shape, accepting that data written by the new columns is dropped.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

from app.db import connect as _connect

logger = logging.getLogger("migrations")


@dataclass(frozen=True)
class Migration:
    """One schema step: version N moves N-1 -> N; down moves N -> N-1."""

    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None]


def _add_note(connection: sqlite3.Connection) -> None:
    # broker_orders.note carries the auto-exit reason (stop-loss, take-profit,
    # max-hold) on orders submitted without a research run behind them (P2.4).
    connection.execute("ALTER TABLE broker_orders ADD COLUMN note TEXT")


def _drop_note(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE broker_orders DROP COLUMN note")


MIGRATIONS: list[Migration] = [
    Migration(1, "broker_orders.note for auto-exit reasons", _add_note, _drop_note),
]

LATEST = len(MIGRATIONS)


def current_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def migrate(connection: sqlite3.Connection, target: int | None = None) -> int:
    """Apply pending up-steps oldest-first, one transaction per step. Returns
    the version the database is at when done."""
    version = current_version(connection)
    target = LATEST if target is None else target
    for migration in MIGRATIONS:
        if migration.version <= version or migration.version > target:
            continue
        migration.up(connection)
        # PRAGMA cannot take a bound parameter; the value only ever comes
        # from the MIGRATIONS list, never from input.
        connection.execute(f"PRAGMA user_version = {migration.version}")
        connection.commit()
        logger.info("[migrations] applied %03d %s", migration.version, migration.name)
        version = migration.version
    return version


def rollback(connection: sqlite3.Connection, target: int = 0) -> int:
    """Reverse applied steps newest-first back to `target` (tested by
    scripts/check_migrations.py). Returns the version the database is at."""
    version = current_version(connection)
    for migration in reversed(MIGRATIONS):
        if migration.version <= target or migration.version > version:
            continue
        migration.down(connection)
        connection.execute(f"PRAGMA user_version = {migration.version - 1}")
        connection.commit()
        logger.info("[migrations] rolled back %03d %s", migration.version, migration.name)
        version = migration.version - 1
    return version


def apply_latest() -> int:
    """Bring the portfolio database to the newest schema; called once per
    startup, after the module inits created any tables the steps alter."""
    with _connect() as connection:
        return migrate(connection)
