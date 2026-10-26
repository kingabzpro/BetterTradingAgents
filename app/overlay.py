"""Risk overlay: deterministic exits between agent runs (release batch 3,
item 13; pattern from FinRL-X).

The agents decide; the overlay protects. Between runs it watches every open
paper position against two mechanical rules - an absolute stop from the
entry price and a trailing stop from the position's peak - and exits what
trips. A stop also starts a cooldown window during which autopilot will not
re-enter that ticker, so a stopped-out name is not bought straight back on
the next optimistic analysis.

State (entry, peak, cooldown) lives in the shared SQLite database under
overlay_state, so it survives restarts and is shared by every caller. The
overlay only ever SELLS through broker.submit_exit_order - the same guarded
path as a human click, minus the run/analysis binding - and with
OVERLAY_ENABLED=0 (the default) it does nothing at all.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.config import settings
from app.db import connect as _connect

logger = logging.getLogger("overlay")


@dataclass
class OverlayExit:
    """One stop decision for one position (executed or, on failure, reported)."""

    ticker: str
    trigger: str  # "absolute_stop" | "trailing_stop"
    entry_price: float
    current_price: float
    peak_price: float
    loss_from_entry_pct: float
    loss_from_peak_pct: float
    notional: float | None = None
    status: str = "signalled"  # signalled | an Alpaca order status | rejected
    error: str | None = None


def _init_db() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS overlay_state (
                ticker TEXT PRIMARY KEY,
                entry_price REAL NOT NULL DEFAULT 0,
                entry_date TEXT NOT NULL DEFAULT '',
                peak_price REAL NOT NULL DEFAULT 0,
                peak_date TEXT NOT NULL DEFAULT '',
                cooldown_until TEXT NOT NULL DEFAULT '',
                stopped_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


async def init() -> None:
    await asyncio.to_thread(_init_db)


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------


def evaluate(
    entry_price: float,
    peak_price: float,
    current_price: float,
    stop_loss_pct: float,
    trailing_stop_pct: float,
) -> str | None:
    """The trigger for one position at one price, or None.

    Absolute stop first: the position has lost stop_loss_pct from entry.
    Trailing stop: it has lost trailing_stop_pct from its own peak (only
    meaningful once the peak is above entry). Returning the trigger makes the
    reason explicit in the order record.
    """
    if entry_price <= 0 or current_price <= 0:
        return None
    if current_price <= entry_price * (1 - stop_loss_pct) + 1e-9:
        return "absolute_stop"
    effective_peak = max(peak_price, entry_price)
    if current_price <= effective_peak * (1 - trailing_stop_pct) + 1e-9:
        return "trailing_stop"
    return None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _read_state(ticker: str) -> dict | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM overlay_state WHERE ticker = ?", (ticker,)
        ).fetchone()
    return dict(row) if row else None


def _write_state(
    ticker: str,
    entry_price: float,
    entry_date: str,
    peak_price: float,
    peak_date: str,
    stopped_at: str = "",
) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO overlay_state (
                ticker, entry_price, entry_date, peak_price, peak_date,
                cooldown_until, stopped_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '', ?, datetime('now'))
            ON CONFLICT(ticker) DO UPDATE SET
                entry_price = excluded.entry_price,
                entry_date = excluded.entry_date,
                peak_price = excluded.peak_price,
                peak_date = excluded.peak_date,
                stopped_at = excluded.stopped_at,
                updated_at = datetime('now')
            """,
            (ticker, entry_price, entry_date, peak_price, peak_date, stopped_at),
        )


def _set_cooldown(ticker: str, until: str) -> None:
    """Upsert: a cooldown can be set before any price state exists (an exit
    recorded out of band, or a test) and must simply work."""
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO overlay_state (ticker, cooldown_until, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(ticker) DO UPDATE SET
                cooldown_until = excluded.cooldown_until,
                updated_at = datetime('now')
            """,
            (ticker, until),
        )


def _forget(ticker: str) -> None:
    """A closed position no longer needs price state; its cooldown stays."""
    with _connect() as connection:
        connection.execute(
            "UPDATE overlay_state SET entry_price = 0, peak_price = 0, peak_date = '', "
            "updated_at = datetime('now') WHERE ticker = ?",
            (ticker,),
        )


def cooldown_until(ticker: str) -> str | None:
    """The ISO date until which re-entry is blocked, or None.

    Expired cooldowns report None (and the NEXT stop rewrites the row), so
    callers never need to clean up.
    """
    state = _read_state(ticker)
    if not state:
        return None
    until = (state.get("cooldown_until") or "").strip()
    if not until:
        return None
    return until if until >= date.today().isoformat() else None


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


async def run_check(execute: bool = False) -> list[OverlayExit]:
    """Evaluate every open position; with execute=True, exit what trips.

    Best-effort per position: one provider or broker failure must not stop the
    scan, and the state upsert always reflects the latest peak. Peaks are
    seeded from entry and then run on the current price; on the first sight of
    a position the max close since entry is used when the history is
    reachable, so a position that ran up before the overlay was switched on
    is not treated as if it peaked at entry.
    """
    from app import broker

    exits: list[OverlayExit] = []
    try:
        positions = await broker.positions()
    except Exception as exc:  # noqa: BLE001 - the overlay never wedges a caller
        logger.warning("[overlay] positions unavailable: %s", exc)
        return []
    held = {p.symbol for p in positions}
    _cleanup_stale(held)

    for position in positions:
        try:
            exits.append(await _check_one(position, execute=execute))
        except Exception as exc:  # noqa: BLE001 - next position still checked
            logger.warning("[overlay] %s check failed: %s", position.symbol, exc)
    return [e for e in exits if e is not None]


async def _check_one(position, execute: bool) -> OverlayExit | None:
    from app import broker

    ticker = position.symbol
    current = position.current_price
    if current is None or current <= 0:
        return None
    state = _read_state(ticker) or {}
    entry = state.get("entry_price") or position.avg_entry_price or 0.0
    peak = state.get("peak_price") or 0.0
    peak_date = state.get("peak_date") or ""
    if peak <= 0:
        peak = await _seed_peak(ticker, position, entry)
        peak_date = date.today().isoformat()

    if current > peak:
        peak, peak_date = current, date.today().isoformat()

    trigger = evaluate(
        entry,
        peak,
        current,
        settings.overlay_stop_loss_pct,
        settings.overlay_trailing_stop_pct,
    )
    entry_date = state.get("entry_date") or ""
    if not entry_date:
        entry_date = datetime.now(timezone.utc).date().isoformat()

    if trigger is None:
        await _to_thread_state(ticker, entry, entry_date, peak, peak_date)
        return None

    exit_note = OverlayExit(
        ticker=ticker,
        trigger=trigger,
        entry_price=round(entry, 4),
        current_price=round(current, 4),
        peak_price=round(peak, 4),
        loss_from_entry_pct=round((current / entry - 1) * 100, 2) if entry else 0.0,
        loss_from_peak_pct=round((current / peak - 1) * 100, 2) if peak else 0.0,
    )
    cooldown_until = (
        date.today() + timedelta(days=settings.overlay_cooldown_days)
    ).isoformat()
    if not execute:
        exit_note.status = "signalled"
        return exit_note

    notional = position.market_value or 0.0
    try:
        order = await broker.submit_exit_order(
            ticker,
            notional,
            reason=(
                f"risk overlay {trigger}: {exit_note.loss_from_entry_pct:+.1f}% "
                f"from entry, {exit_note.loss_from_peak_pct:+.1f}% from peak"
            ),
        )
        exit_note.status = order.status
        exit_note.notional = order.notional
    except Exception as exc:  # noqa: BLE001 - report, keep scanning
        logger.warning("[overlay] %s exit rejected: %s", ticker, exc)
        exit_note.status = "rejected"
        exit_note.error = str(exc)[:300]
    # Cooldown and stopped_at land regardless of the order outcome: after a
    # stop signal, a re-entry is a fresh decision, never an accident.
    await _to_thread_state(
        ticker, entry, entry_date, peak, peak_date,
        stopped_at=datetime.now(timezone.utc).isoformat(),
    )
    await _to_thread_cooldown(ticker, cooldown_until)
    logger.info(
        "[overlay] %s %s at %.2f (%+.1f%% from entry) -> %s",
        ticker, trigger, current, exit_note.loss_from_entry_pct, exit_note.status,
    )
    return exit_note


async def _seed_peak(ticker: str, position, entry: float) -> float:
    """Best-effort historical peak: max close since the position was opened."""
    peak = max(entry, position.current_price or 0.0)
    try:
        from app.tools.market_data import get_closes_between

        entry_date = _position_entry_date(ticker)
        closes = await get_closes_between(
            ticker, entry_date, date.today().isoformat()
        )
        if closes:
            peak = max(peak, max(closes.values()))
    except Exception as exc:  # noqa: BLE001 - seeding is best-effort
        logger.warning("[overlay] %s peak seed failed: %s", ticker, exc)
    return round(peak, 4)


def _position_entry_date(ticker: str) -> str:
    """Approximate the position's entry date from the earliest local buy order
    for the ticker; falls back to 90 days ago when history is thin."""
    fallback = (date.today() - timedelta(days=90)).isoformat()
    try:
        with _connect() as connection:
            row = connection.execute(
                "SELECT MIN(date(created_at)) AS day FROM broker_orders "
                "WHERE ticker = ? AND side = 'buy' AND status IN ('filled', 'pending_submit')",
                (ticker,),
            ).fetchone()
        return (row["day"] or fallback) if row else fallback
    except Exception:  # noqa: BLE001
        return fallback


def _cleanup_stale(held: set[str]) -> None:
    """Forget price state for tickers with no open position (cooldowns stay)."""
    try:
        with _connect() as connection:
            rows = connection.execute(
                "SELECT ticker FROM overlay_state WHERE entry_price > 0"
            ).fetchall()
        for row in rows:
            if row["ticker"] not in held:
                _forget(row["ticker"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[overlay] stale-state cleanup failed: %s", exc)


async def _to_thread_state(ticker, entry, entry_date, peak, peak_date, stopped_at=""):
    await asyncio.to_thread(
        _write_state, ticker, entry, entry_date, peak, peak_date, stopped_at
    )


async def _to_thread_cooldown(ticker: str, until: str) -> None:
    await asyncio.to_thread(_set_cooldown, ticker, until)
