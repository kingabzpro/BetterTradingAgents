"""SQLite cache for point-in-time market snapshots (ROADMAP 2.2).

Keyed by ticker + decision date + window params, so re-running a backtest
with a warm cache makes zero network calls. Backtest overrides live in env:
BACKTEST_CACHE (cache db path) and BACKTEST_OFFLINE=1 (cache misses raise
instead of hitting the network - proves warm-cache runs are truly offline).
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "backtests" / "cache.db"
)


def cache_path() -> Path:
    return Path(os.environ.get("BACKTEST_CACHE", str(DEFAULT_DB)))


def offline_mode() -> bool:
    return os.environ.get("BACKTEST_OFFLINE", "").strip() in ("1", "true", "yes")


class SnapshotCache:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    ticker TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    lookback_days INTEGER NOT NULL,
                    news_days INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (ticker, as_of, lookback_days, news_days)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fundamentals (
                    ticker TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS price_series (
                    ticker TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    closes TEXT NOT NULL,
                    PRIMARY KEY (ticker, start, end)
                )
                """
            )

    # -- as-of snapshots ------------------------------------------------------

    def get_snapshot(
        self, ticker: str, as_of: str, lookback_days: int, news_days: int
    ) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM snapshots WHERE ticker = ? AND as_of = ? "
                "AND lookback_days = ? AND news_days = ?",
                (ticker, as_of, lookback_days, news_days),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def put_snapshot(
        self,
        ticker: str,
        as_of: str,
        lookback_days: int,
        news_days: int,
        payload: dict,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(ticker, as_of, lookback_days, news_days, payload, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ticker,
                    as_of,
                    lookback_days,
                    news_days,
                    json.dumps(payload),
                    datetime.now(timezone.utc).timestamp(),
                ),
            )

    # -- current-vintage fundamentals (known bias, fetched once per ticker) ---

    def get_fundamentals(self, ticker: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM fundamentals WHERE ticker = ?", (ticker,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def put_fundamentals(self, ticker: str, payload: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO fundamentals (ticker, payload, fetched_at) "
                "VALUES (?, ?, ?)",
                (
                    ticker,
                    json.dumps(payload),
                    datetime.now(timezone.utc).timestamp(),
                ),
            )

    # -- close series for grading ----------------------------------------------

    def get_series(self, ticker: str, start: str, end: str) -> dict[str, float] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT closes FROM price_series WHERE ticker = ? AND start = ? "
                "AND end = ?",
                (ticker, start, end),
            ).fetchone()
        return json.loads(row["closes"]) if row else None

    def put_series(self, ticker: str, start: str, end: str, closes: dict[str, float]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO price_series (ticker, start, end, closes) "
                "VALUES (?, ?, ?, ?)",
                (ticker, start, end, json.dumps(closes)),
            )
