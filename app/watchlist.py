"""SQLite-backed watchlist with decision-change detection (ROADMAP P1.4).

A watchlist row stores the ticker, an optional note, preferred outlook/depth,
and a baseline call (usually the analysis the user saved). Listing compares
that baseline to the newest completed run for the same owner and ticker:

- no newer completed result  -> not_reanalyzed
- newer result, same decision and evidence bucket -> no_change
- decision or evidence bucket moved -> changed

Manual refresh only: no scheduler, email, or push. Price move uses a live
quote when available and stays None when it is not - never a fake zero.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Literal

from app.config import settings
from app.depth import DEFAULT_DEPTH, normalize_depth
from app.models import StockAnalysis, WatchlistCall, WatchlistItem
from app.outlook import DEFAULT_OUTLOOK, normalize_outlook
from app.tools.market_data import get_current_price

logger = logging.getLogger("watchlist")

TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL DEFAULT '',
                ticker TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                outlook TEXT NOT NULL DEFAULT 'short_term',
                depth TEXT NOT NULL DEFAULT 'medium',
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_run_id TEXT,
                last_analyzed_at REAL,
                last_decision TEXT,
                last_confidence REAL,
                last_price REAL,
                last_as_of TEXT,
                last_risk_flags TEXT NOT NULL DEFAULT '[]',
                UNIQUE(owner_id, ticker)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_watchlist_owner "
            "ON watchlist(owner_id, ticker)"
        )


async def init() -> None:
    await asyncio.to_thread(_init_db)


def evidence_bucket(confidence: float | None) -> str:
    """Match the UI's convictionLabel thresholds (P1.1)."""
    if confidence is None:
        return "unknown"
    pct = round((confidence or 0) * 100)
    if pct >= 70:
        return "strong"
    if pct >= 50:
        return "moderate"
    return "low"


def _call_from_analysis(
    analysis: StockAnalysis,
    run_id: str,
    analyzed_at: float | None,
) -> WatchlistCall:
    return WatchlistCall(
        run_id=run_id,
        analyzed_at=analyzed_at,
        decision=None if analysis.error else analysis.decision,
        confidence=None if analysis.error else analysis.confidence,
        price=analysis.price,
        as_of=analysis.as_of,
        risk_flags=list(analysis.risk_flags),
    )


def _baseline_from_row(row: sqlite3.Row) -> WatchlistCall | None:
    if not row["last_run_id"] and not row["last_decision"]:
        return None
    try:
        flags = json.loads(row["last_risk_flags"] or "[]")
    except (json.JSONDecodeError, TypeError):
        flags = []
    return WatchlistCall(
        run_id=row["last_run_id"] or "",
        analyzed_at=row["last_analyzed_at"],
        decision=row["last_decision"],
        confidence=row["last_confidence"],
        price=row["last_price"],
        as_of=row["last_as_of"] or "",
        risk_flags=flags if isinstance(flags, list) else [],
    )


def _insert(
    owner_id: str,
    ticker: str,
    note: str,
    outlook: str,
    depth: str,
    baseline: WatchlistCall | None,
) -> int:
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO watchlist (
                owner_id, ticker, note, outlook, depth,
                last_run_id, last_analyzed_at, last_decision, last_confidence,
                last_price, last_as_of, last_risk_flags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, ticker) DO UPDATE SET
                note = excluded.note,
                outlook = excluded.outlook,
                depth = excluded.depth,
                last_run_id = COALESCE(excluded.last_run_id, watchlist.last_run_id),
                last_analyzed_at = COALESCE(excluded.last_analyzed_at, watchlist.last_analyzed_at),
                last_decision = COALESCE(excluded.last_decision, watchlist.last_decision),
                last_confidence = COALESCE(excluded.last_confidence, watchlist.last_confidence),
                last_price = COALESCE(excluded.last_price, watchlist.last_price),
                last_as_of = COALESCE(excluded.last_as_of, watchlist.last_as_of),
                last_risk_flags = CASE
                    WHEN excluded.last_run_id IS NOT NULL THEN excluded.last_risk_flags
                    ELSE watchlist.last_risk_flags
                END
            """,
            (
                owner_id,
                ticker,
                note,
                normalize_outlook(outlook),
                normalize_depth(depth),
                baseline.run_id if baseline else None,
                baseline.analyzed_at if baseline else None,
                baseline.decision if baseline else None,
                baseline.confidence if baseline else None,
                baseline.price if baseline else None,
                baseline.as_of if baseline else None,
                json.dumps(baseline.risk_flags if baseline else []),
            ),
        )
        row = connection.execute(
            "SELECT id FROM watchlist WHERE owner_id = ? AND ticker = ?",
            (owner_id, ticker),
        ).fetchone()
        return int(cursor.lastrowid or row["id"])


def _load_baseline(owner_id: str, ticker: str) -> dict | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM watchlist WHERE owner_id = ? AND ticker = ?",
            (owner_id, ticker),
        ).fetchone()
        return dict(row) if row else None


async def _newest_result(
    owner_id: str, ticker: str, baseline: WatchlistCall | None
) -> tuple[WatchlistCall | None, float | None]:
    """Newest completed result for this ticker that is not the baseline run."""
    from app import run_history

    runs = await run_history.list_runs(owner_id, limit=100)
    baseline_run = baseline.run_id if baseline else None
    baseline_at = baseline.analyzed_at if baseline else None
    best: WatchlistCall | None = None
    best_at = -1.0
    for item in runs:
        if item.status != "completed" or ticker not in item.tickers:
            continue
        if item.run_id == baseline_run:
            continue
        # Prefer strictly newer starts; a missing baseline accepts any run.
        if baseline_at is not None and item.started_at <= baseline_at:
            continue
        status = await run_history.get(item.run_id)
        if status is None:
            continue
        analysis = status.results.get(ticker)
        if analysis is None or analysis.error:
            continue
        call = _call_from_analysis(analysis, item.run_id, item.started_at)
        if item.started_at > best_at:
            best = call
            best_at = item.started_at
    return best, (best_at if best is not None else None)


def _age_hours(as_of: str) -> float | None:
    if not as_of:
        return None
    try:
        stamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0)


def _compose(
    row: dict,
    baseline: WatchlistCall | None,
    current: WatchlistCall | None,
    live_price: float | None,
) -> WatchlistItem:
    status: Literal["not_reanalyzed", "no_change", "changed"] = "not_reanalyzed"
    decision_changed = False
    evidence_changed = False
    unresolved: list[str] = []

    if baseline is not None and current is not None:
        decision_changed = bool(
            baseline.decision
            and current.decision
            and baseline.decision != current.decision
        )
        evidence_changed = evidence_bucket(baseline.confidence) != evidence_bucket(
            current.confidence
        )
        status = "changed" if decision_changed or evidence_changed else "no_change"
        unresolved = list(current.risk_flags)
    elif baseline is not None:
        unresolved = list(baseline.risk_flags)
    elif current is not None:
        unresolved = list(current.risk_flags)

    price_move: float | None = None
    anchor_price = (current or baseline).price if (current or baseline) else None
    if anchor_price and live_price and anchor_price > 0:
        price_move = round((live_price / anchor_price - 1) * 100, 2)

    age_source = current or baseline
    data_age = _age_hours(age_source.as_of) if age_source else None
    view = current or baseline

    return WatchlistItem(
        id=int(row["id"]),
        ticker=row["ticker"],
        note=row["note"] or "",
        outlook=normalize_outlook(row["outlook"]),
        depth=normalize_depth(row["depth"]),
        added_at=row["added_at"] or "",
        last_call=baseline,
        current_call=current,
        change_status=status,
        decision_changed=decision_changed,
        evidence_changed=evidence_changed,
        price_move_pct=price_move,
        live_price=live_price,
        data_age_hours=data_age,
        unresolved_risk_flags=unresolved,
        view_run_id=view.run_id if view and view.run_id else None,
    )


def _select_rows(owner_id: str) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM watchlist WHERE owner_id = ? ORDER BY ticker",
            (owner_id,),
        ).fetchall()
        return [dict(row) for row in rows]


async def list_watchlist(owner_id: str) -> list[WatchlistItem]:
    rows = await asyncio.to_thread(_select_rows, owner_id)
    items: list[WatchlistItem] = []
    for row in rows:
        baseline = await asyncio.to_thread(_baseline_from_row, row)
        current, _ = await _newest_result(owner_id, row["ticker"], baseline)
        live = await get_current_price(row["ticker"])
        items.append(await asyncio.to_thread(_compose, row, baseline, current, live))
    return items


async def add_item(
    owner_id: str,
    ticker: str,
    note: str = "",
    outlook: str = DEFAULT_OUTLOOK,
    depth: str = DEFAULT_DEPTH,
    run_id: str | None = None,
) -> tuple[WatchlistItem, bool]:
    ticker = ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        raise ValueError(f"invalid ticker symbol: '{ticker}'")
    existing = await asyncio.to_thread(_load_baseline, owner_id, ticker)
    already = existing is not None

    baseline: WatchlistCall | None = None
    if run_id:
        from app import run_history

        status = await run_history.get(run_id)
        if status is None:
            raise LookupError(f"run {run_id} not found")
        analysis = status.results.get(ticker)
        if analysis is None:
            raise LookupError(f"ticker {ticker} is not in run {run_id}")
        if analysis.error:
            raise ValueError(f"cannot watch {ticker}: that analysis failed")
        baseline = _call_from_analysis(analysis, run_id, status.started_at)
        # Prefer the run's horizon when snapshotting so change detection stays comparable.
        outlook = status.outlook
        depth = status.depth
    elif existing and existing.get("last_run_id"):
        # Re-save without a run keeps the prior baseline (COALESCE on update).
        baseline = None

    await asyncio.to_thread(
        _insert, owner_id, ticker, note.strip(), outlook, depth, baseline
    )
    row = await asyncio.to_thread(_load_baseline, owner_id, ticker)
    if row is None:  # pragma: no cover - insert just wrote it
        raise RuntimeError("watchlist insert failed")
    base = await asyncio.to_thread(_baseline_from_row, row)
    current, _ = await _newest_result(owner_id, ticker, base)
    live = await get_current_price(ticker)
    item = await asyncio.to_thread(_compose, row, base, current, live)
    return item, already


async def update_item(
    owner_id: str, item_id: int, note: str | None, outlook: str | None, depth: str | None
) -> WatchlistItem:
    def _update() -> dict | None:
        with _connect() as connection:
            row = connection.execute(
                "SELECT * FROM watchlist WHERE id = ? AND owner_id = ?",
                (item_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE watchlist SET note = ?, outlook = ?, depth = ? "
                "WHERE id = ? AND owner_id = ?",
                (
                    (row["note"] if note is None else note.strip())[:500],
                    normalize_outlook(outlook if outlook is not None else row["outlook"]),
                    normalize_depth(depth if depth is not None else row["depth"]),
                    item_id,
                    owner_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM watchlist WHERE id = ?", (item_id,)
            ).fetchone()
            return dict(updated) if updated else None

    row = await asyncio.to_thread(_update)
    if row is None:
        raise LookupError(f"no watchlist item with id {item_id}")
    baseline = await asyncio.to_thread(_baseline_from_row, row)
    current, _ = await _newest_result(owner_id, row["ticker"], baseline)
    live = await get_current_price(row["ticker"])
    return await asyncio.to_thread(_compose, row, baseline, current, live)


async def remove_item(owner_id: str, item_id: int) -> str:
    def _delete() -> str | None:
        with _connect() as connection:
            row = connection.execute(
                "SELECT ticker FROM watchlist WHERE id = ? AND owner_id = ?",
                (item_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM watchlist WHERE id = ? AND owner_id = ?", (item_id, owner_id)
            )
            return str(row["ticker"])

    ticker = await asyncio.to_thread(_delete)
    if ticker is None:
        raise LookupError(f"no watchlist item with id {item_id}")
    return ticker


async def clear(owner_id: str) -> int:
    def _clear() -> int:
        with _connect() as connection:
            cursor = connection.execute(
                "DELETE FROM watchlist WHERE owner_id = ?", (owner_id,)
            )
            return int(cursor.rowcount or 0)

    return await asyncio.to_thread(_clear)
