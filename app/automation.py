"""Automated paper-trading sessions (autopilot).

The loop turns the app's own pipeline into a scheduled trader. Each session:
discover the strongest candidates (the same research-backed screen behind
"I Am Feeling Lucky"), add every held ticker so SELL decisions can act on
open positions, run the full agent workflow as one ordinary run (owner
"automation", visible in run history and via every order's decision link),
then submit the trades it agrees with through broker.submit_order - the
same guarded path a human click uses, so the kill switch, per-order and
daily caps, decision-freshness check, and anti-short haircut all still
apply. Sizing comes from the risk gate's suggested_size_usd; BUYs also
need at least AUTOMATION_MIN_CONFIDENCE and are skipped for tickers
already held (autopilot never adds to a position); SELLs only exit
positions actually held.

Nothing trades unless every layer is on: Alpaca keys configured, the
ALPACA_TRADING_ENABLED kill switch up, a real LLM configured (mock
decisions never trade), and the toggle enabled (DB, seeded from
AUTOMATION_ENABLED). Scheduled sessions additionally wait for the market
to open; a manual "run now" may trade a closed-market session and those
orders simply queue for the next open.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time

from app.config import settings
from app.depth import normalize_depth
from app.models import (
    AutomationOrderOutcome,
    AutomationSession,
    AutomationStatus,
    StockAnalysis,
)
from app.outlook import normalize_outlook

logger = logging.getLogger("automation")

SESSIONS_KEPT = 200  # prune the local history; orders and runs live forever
MAX_SESSION_TICKERS = 10  # cost brake: candidates + held, truncated with a note
POLL_SECONDS = 15.0  # loop granularity; makes the toggle responsive

_loop_task: asyncio.Task | None = None
_session_task: asyncio.Task | None = None
_session_active = False
_next_check_at: float | None = None


# ---- local tables -----------------------------------------------------------


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db() -> None:
    now = time.time()
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO automation_state (id, enabled, updated_at) "
            "VALUES (1, ?, ?)",
            (int(settings.automation_enabled), now),
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger TEXT NOT NULL DEFAULT 'schedule',
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                run_id TEXT,
                tickers_json TEXT NOT NULL DEFAULT '[]',
                summary_json TEXT NOT NULL DEFAULT '{}',
                error TEXT
            )
            """
        )
        # A crashed process leaves sessions 'running' forever; settle them
        # honestly, exactly like interrupted analysis runs.
        connection.execute(
            "UPDATE automation_sessions SET status = 'failed', finished_at = ?, "
            "error = COALESCE(error, 'server restarted mid-session') "
            "WHERE status = 'running'",
            (now,),
        )


async def init() -> None:
    await asyncio.to_thread(_init_db)
    if await enabled():
        logger.info(
            "[automation] enabled | every %d min while the market is open "
            "(candidates=%d, min_confidence=%.2f, depth=%s)",
            settings.automation_interval_minutes,
            settings.automation_candidates,
            settings.automation_min_confidence,
            settings.automation_depth,
        )
    else:
        logger.info("[automation] disabled")


def _read_enabled() -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT enabled FROM automation_state WHERE id = 1"
        ).fetchone()
    return bool(row and row["enabled"])


async def enabled() -> bool:
    return await asyncio.to_thread(_read_enabled)


def _write_enabled(value: bool) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE automation_state SET enabled = ?, updated_at = ? WHERE id = 1",
            (int(value), time.time()),
        )


async def set_enabled(value: bool) -> None:
    await asyncio.to_thread(_write_enabled, value)


def _insert_session(trigger: str, started_at: float) -> int:
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO automation_sessions (trigger, status, started_at) "
            "VALUES (?, 'running', ?)",
            (trigger, started_at),
        )
        connection.execute(
            f"DELETE FROM automation_sessions WHERE id NOT IN "
            f"(SELECT id FROM automation_sessions ORDER BY id DESC LIMIT {SESSIONS_KEPT})"
        )
        return int(cursor.lastrowid)


def _finish_session(
    session_id: int,
    status: str,
    run_id: str | None,
    tickers: list[str],
    decisions: dict[str, str],
    orders: list[AutomationOrderOutcome],
    error: str | None,
) -> None:
    summary = {
        "decisions": decisions,
        "orders": [order.model_dump() for order in orders],
    }
    with _connect() as connection:
        connection.execute(
            "UPDATE automation_sessions SET status = ?, finished_at = ?, run_id = ?, "
            "tickers_json = ?, summary_json = ?, error = ? WHERE id = ?",
            (
                status,
                time.time(),
                run_id,
                json.dumps(tickers),
                json.dumps(summary),
                error,
                session_id,
            ),
        )


def _list_sessions(limit: int = 10) -> list[AutomationSession]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM automation_sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    sessions = []
    for row in rows:
        try:
            summary = json.loads(row["summary_json"] or "{}")
            orders = [AutomationOrderOutcome.model_validate(o) for o in summary.get("orders", [])]
        except ValueError:
            summary, orders = {}, []
        sessions.append(
            AutomationSession(
                id=row["id"],
                trigger=row["trigger"],
                status=row["status"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                run_id=row["run_id"],
                tickers=json.loads(row["tickers_json"] or "[]"),
                decisions=summary.get("decisions", {}),
                orders=orders,
                error=row["error"],
            )
        )
    return sessions


async def status_snapshot() -> AutomationStatus:
    return AutomationStatus(
        enabled=await enabled(),
        session_active=_session_active,
        configured=settings.alpaca_configured,
        trading_enabled=settings.alpaca_trading_enabled,
        llm_configured=settings.llm_configured,
        interval_minutes=settings.automation_interval_minutes,
        candidates=settings.automation_candidates,
        min_confidence=settings.automation_min_confidence,
        outlook=normalize_outlook(settings.automation_outlook),
        depth=normalize_depth(settings.automation_depth),
        allow_sells=settings.automation_allow_sells,
        next_check_at=_next_check_at,
        sessions=await asyncio.to_thread(_list_sessions),
    )


# ---- one session ------------------------------------------------------------


def _skip(
    trigger: str, reason: str, started_at: float | None = None
) -> AutomationSession:
    """Record a session that did not trade, with the reason why."""
    started = started_at if started_at is not None else time.time()
    session_id = _insert_session(trigger, started)
    _finish_session(session_id, "skipped", None, [], {}, [], reason)
    logger.info("[automation] session skipped: %s", reason)
    return AutomationSession(
        id=session_id, trigger=trigger, status="skipped", started_at=started,
        finished_at=time.time(), error=reason,
    )


def _final_decisions(results: dict[str, StockAnalysis]) -> dict[str, str]:
    return {
        ticker: analysis.decision
        for ticker, analysis in results.items()
        if not analysis.error
    }


async def run_session(trigger: str = "schedule") -> AutomationSession:
    """One full scan -> analyze -> order cycle. Never raises."""
    global _session_active

    if _session_active:
        return _skip(trigger, "another session is already running")

    started_at = time.time()
    if not settings.alpaca_configured:
        return _skip(trigger, "Alpaca paper trading is not configured", started_at)
    if not settings.alpaca_trading_enabled:
        return _skip(trigger, "order submissions are disabled (kill switch)", started_at)
    if not settings.llm_configured:
        return _skip(trigger, "no LLM configured; mock decisions never trade", started_at)

    from app import broker

    try:
        clock = await broker.clock()
    except Exception as exc:  # noqa: BLE001 - a broker failure must not kill the loop
        return _skip(trigger, f"market clock unavailable: {exc}", started_at)
    if trigger == "schedule" and not clock["is_open"]:
        return _skip(
            trigger, f"market closed until {clock.get('next_open') or 'the next open'}",
            started_at,
        )

    try:
        held = await broker.positions()
    except Exception as exc:  # noqa: BLE001
        return _skip(trigger, f"paper positions unavailable: {exc}", started_at)
    held_map = {position.symbol: position for position in held}

    candidates: list[str] = []
    discovery_note = ""
    try:
        from app.discovery import discover_stocks

        discovery = await discover_stocks(
            normalize_outlook(settings.automation_outlook),
            settings.automation_candidates,
        )
        candidates = [t for t in discovery["tickers"] if t not in held_map]
    except Exception as exc:  # noqa: BLE001 - fall back to a held-only session
        discovery_note = f"discovery failed: {exc}"

    tickers = list(dict.fromkeys(candidates + list(held_map)))[:MAX_SESSION_TICKERS]
    if not tickers:
        return _skip(
            trigger, discovery_note or "no candidates and no held positions", started_at
        )
    if discovery_note:
        logger.warning(
            "[automation] %s; proceeding with held tickers only", discovery_note
        )

    from app.runs import store

    session_id = _insert_session(trigger, started_at)
    _session_active = True
    orders: list[AutomationOrderOutcome] = []
    status = "completed"
    error: str | None = None
    run_id: str | None = None
    decisions: dict[str, str] = {}
    run = None
    try:
        run = await store.create(
            tickers,
            client_id="automation",
            outlook=settings.automation_outlook,
            depth=settings.automation_depth,
        )
        run_id = run.run_id
        try:
            await asyncio.wait_for(
                run.execution_task,
                timeout=settings.automation_session_timeout_minutes * 60,
            )
        except asyncio.TimeoutError:
            await store.cancel(run.run_id)
            status = "failed"
            error = (
                f"session timed out after {settings.automation_session_timeout_minutes} min; "
                "the run was cancelled"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            error = f"analysis run failed: {exc}"

        if run is not None:
            results = run.to_status().results
            decisions = _final_decisions(results)
            if status == "completed":
                logger.info(
                    "[automation] run %s done: %s",
                    run.run_id,
                    ", ".join(f"{t}={a.decision}" for t, a in results.items()),
                )
                for ticker, analysis in results.items():
                    if analysis.error:
                        continue
                    order = await _act_on_decision(
                        run.run_id, ticker, analysis, held_map
                    )
                    if order is not None:
                        orders.append(order)
                if not orders:
                    logger.info(
                        "[automation] run %s: no orders this session", run.run_id
                    )
    except asyncio.CancelledError:
        status, error = "failed", "session cancelled"
        raise
    finally:
        _session_active = False
        _finish_session(session_id, status, run_id, tickers, decisions, orders, error)
    return AutomationSession(
        id=session_id,
        trigger=trigger,
        status=status,
        started_at=started_at,
        finished_at=time.time(),
        run_id=run_id,
        tickers=tickers,
        decisions=decisions,
        orders=orders,
        error=error,
    )


async def _act_on_decision(
    run_id: str, ticker: str, analysis: StockAnalysis, held_map: dict
) -> AutomationOrderOutcome | None:
    """Translate one risk-gated decision into an order attempt, or None when
    the decision is nothing to act on (HOLD, weak BUY, SELL with sells off)."""
    from app import broker

    if analysis.decision == "BUY":
        if analysis.confidence < settings.automation_min_confidence:
            logger.info(
                "[automation] %s BUY %.0f%% below the %.0f%% bar",
                ticker, analysis.confidence * 100,
                settings.automation_min_confidence * 100,
            )
            return None
        if ticker in held_map:
            logger.info("[automation] %s already held; never adding to a position", ticker)
            return None
        notional = analysis.suggested_size_usd or settings.default_position_size
        side = "buy"
    elif analysis.decision == "SELL" and settings.automation_allow_sells:
        position = held_map.get(ticker)
        if position is None:
            return None  # the broker would reject a short; nothing to exit
        notional = position.market_value
        if notional is None or notional <= 0:
            return AutomationOrderOutcome(
                ticker=ticker, side="sell", status="skipped",
                error="held position has no market value to sell",
            )
        side = "sell"
    else:
        return None

    try:
        order = await broker.submit_order(run_id, ticker, side, notional)
    except Exception as exc:  # noqa: BLE001 - one rejected order must not kill the session
        logger.warning("[automation] %s %s rejected: %s", side, ticker, exc)
        return AutomationOrderOutcome(
            ticker=ticker, side=side, notional=notional, status="rejected",
            error=str(exc)[:300],
        )
    logger.info(
        "[automation] %s %s $%.2f -> %s", side, ticker, notional, order.status
    )
    return AutomationOrderOutcome(
        ticker=ticker,
        side=side,
        notional=notional,
        status=order.status,
        client_order_id=order.client_order_id,
    )


# ---- the loop ---------------------------------------------------------------


async def _loop() -> None:
    global _next_check_at
    logger.info("[automation] loop started")
    while True:
        try:
            if await enabled() and not _session_active:
                _next_check_at = time.time()
                await run_session("schedule")
            _next_check_at = time.time() + settings.automation_interval_minutes * 60
            deadline = _next_check_at
            while (remaining := deadline - time.time()) > 0:
                await asyncio.sleep(min(POLL_SECONDS, remaining))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop outlives any bug
            logger.error("[automation] loop iteration failed: %s", exc)
            _next_check_at = time.time() + 60
            await asyncio.sleep(60)


def start() -> None:
    """Spawn the background loop (idempotent; called from app startup)."""
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(_loop(), name="automation-loop")


async def start_manual_session() -> bool:
    """Fire one session now ('Run now'); False when one is already running."""
    global _session_task
    if _session_active:
        return False
    _session_task = asyncio.create_task(run_session("manual"), name="automation-manual")
    return True
