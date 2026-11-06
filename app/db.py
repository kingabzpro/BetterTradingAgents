"""One shared SQLite connection: every local table lives in the portfolio
database, so all modules connect identically."""

import sqlite3

from app.config import settings


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    # WAL (persistent once set) lets the SSE-heavy reads and the run/memory
    # writes overlap instead of failing with "database is locked", and a
    # busy timeout turns the remaining contention into a short wait rather
    # than a swallowed exception that silently drops a decision row (P2.10).
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection
