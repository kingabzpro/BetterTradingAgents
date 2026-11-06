"""Automated paper-trading sessions (autopilot).

The loop turns the app's own pipeline into a scheduled trader. Each session:
discover the strongest candidates (the same research-backed screen behind
"I Am Feeling Lucky", with names analyzed in the last
AUTOMATION_CANDIDATE_COOLDOWN_DAYS days rotated out so the shortlist does
not repeat for weeks; 0 disables the rotation), add every held ticker so
SELL decisions can act on open positions, run the full agent workflow as
one ordinary run (owner "automation", visible in run history and via every
order's decision link), then submit the trades it agrees with through
broker.submit_order - the same guarded path a human click uses, so the
kill switch, per-order and daily caps, decision-freshness check, and
anti-short haircut all still apply. Sizing comes from the risk gate's
suggested_size_usd; BUYs also need at least AUTOMATION_MIN_CONFIDENCE and
are skipped for tickers already held (autopilot never adds to a position);
SELLs only exit positions actually held.

Nothing trades unless every layer is on: Alpaca keys configured, the
ALPACA_TRADING_ENABLED kill switch up, a real LLM configured (mock
decisions never trade), and the toggle enabled (DB, seeded from
AUTOMATION_ENABLED). Scheduled sessions additionally wait for the market
to open; a manual "run now" may trade a closed-market session and those
orders simply queue for the next open.

Three deterministic brakes ride on top (wiki Roadmap P2.4): BUYs stop once
autopilot's net deployed notional passes AUTOMATION_MAX_DEPLOYED_PCT of
equity or would take cash below AUTOMATION_CASH_FLOOR_PCT; while SPY sits
below its 200-day average BUYs are recorded as braked (research and exits
continue); and WEBHOOK_URL, when set, receives one plain-text POST per
finished session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, timedelta

from app.config import settings
from app.depth import normalize_depth
from app.models import (
    AutomationOrderOutcome,
    AutomationSession,
    AutomationStatus,
    StockAnalysis,
)
from app.outlook import normalize_outlook
from app.redact import redact
from app.tools.http_client import get_client
from app.db import connect as _connect

logger = logging.getLogger("automation")

SESSIONS_KEPT = 200  # prune the local history; orders and runs live forever
MAX_SESSION_TICKERS = 10  # cost brake: candidates + held, truncated with a note
POLL_SECONDS = 15.0  # loop granularity; makes the toggle responsive

_loop_task: asyncio.Task | None = None
_session_task: asyncio.Task | None = None
# The real mutex for "a session is running" (P2.8). The old boolean was only
# set after three network calls, so two triggers (a schedule tick and a manual
# "Run now") could both pass the gate and trade at once. Held for the whole
# session; check .locked() instead of reading a flag.
_session_lock = asyncio.Lock()
_next_check_at: float | None = None


# ---- local tables -----------------------------------------------------------




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
            "(candidates=%d, min_confidence=%.2f, depth=%s, candidate_cooldown=%dd)",
            settings.automation_interval_minutes,
            settings.automation_candidates,
            settings.automation_min_confidence,
            settings.automation_depth,
            settings.automation_candidate_cooldown_days,
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


def _recent_candidate_tickers(cutoff: float) -> list[str]:
    """Tickers analyzed by sessions that started after `cutoff` (deduped).

    Feeds the candidate rotation: names the loop already looked at recently
    are held out of the next shortlist, so sessions explore the rest of the
    screen instead of re-running the same deterministic top names."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT tickers_json FROM automation_sessions WHERE started_at >= ?",
            (cutoff,),
        ).fetchall()
    recent: dict[str, None] = {}
    for row in rows:
        try:
            tickers = json.loads(row["tickers_json"] or "[]")
        except ValueError:
            continue
        if not isinstance(tickers, list):
            continue
        for ticker in tickers:
            recent.setdefault(str(ticker).upper(), None)
    return list(recent)


def _list_sessions(limit: int = 20) -> list[AutomationSession]:
    """Newest sessions for the portfolio card: the top three rows are visible,
    the rest scroll, so the card sends up to 20 rows."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM automation_sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    sessions = []
    for row in rows:
        try:
            summary = json.loads(row["summary_json"] or "{}")
            orders = [AutomationOrderOutcome.model_validate(o) for o in summary.get("orders", [])]
            tickers = json.loads(row["tickers_json"] or "[]")
        except ValueError:
            # A corrupt row renders as an empty session instead of blanking
            # the whole card (P2.8).
            summary, orders, tickers = {}, [], []
        sessions.append(
            AutomationSession(
                id=row["id"],
                trigger=row["trigger"],
                status=row["status"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                run_id=row["run_id"],
                tickers=tickers,
                decisions=summary.get("decisions", {}),
                orders=orders,
                error=row["error"],
            )
        )
    return sessions


async def status_snapshot() -> AutomationStatus:
    return AutomationStatus(
        enabled=await enabled(),
        session_active=_session_lock.locked(),
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


async def _skip(
    trigger: str, reason: str, started_at: float | None = None
) -> AutomationSession:
    """Record a session that did not trade, with the reason why. The writes
    run off the event loop so a slow disk never blocks the API (P2.8)."""
    started = started_at if started_at is not None else time.time()
    session_id = await asyncio.to_thread(_insert_session, trigger, started)
    await asyncio.to_thread(
        _finish_session, session_id, "skipped", None, [], {}, [], reason
    )
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


def _automation_deployed() -> float:
    """Net USD autopilot has deployed: its non-rejected BUY notional minus its
    filled SELL notional (orders whose run belongs to autopilot, owner_id
    'automation'). Conservative on purpose - a pending or unknown buy still
    consumes the budget, an unfilled exit frees nothing (wiki Roadmap P2.4)."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT o.side AS side, SUM(o.notional) AS total
            FROM broker_orders o
            JOIN analysis_runs r ON r.run_id = o.run_id
            WHERE r.owner_id = 'automation'
              AND ((o.side = 'buy' AND o.status NOT IN ('rejected', 'canceled', 'expired'))
                   OR (o.side = 'sell' AND o.status = 'filled'))
            GROUP BY o.side
            """
        ).fetchall()
    totals = {row["side"]: float(row["total"] or 0.0) for row in rows}
    return max(0.0, totals.get("buy", 0.0) - totals.get("sell", 0.0))


async def _bear_market() -> bool | None:
    """True when SPY sits below its 200-day average (wiki Roadmap P2.4).
    None when the answer is unknown - too little history or a fetch failure -
    and the brake then fails open: a data outage must not silently halt
    exits-and-research sessions, and a BUY still passes every other guard."""
    from app.tools.market_data import get_closes_between

    start = (date.today() - timedelta(days=310)).isoformat()
    end = (date.today() + timedelta(days=1)).isoformat()
    try:
        closes = await get_closes_between("SPY", start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[automation] bear-market check failed (not braking): %s", exc)
        return None
    if len(closes) < 200:
        logger.warning(
            "[automation] only %d SPY closes, need 200; not braking", len(closes)
        )
        return None
    window = [closes[day] for day in sorted(closes)[-200:]]
    return window[-1] < sum(window) / len(window)


async def _post_webhook(session_outcome: "AutomationSession") -> None:
    """One plain-text POST per finished session (completed or failed; wiki
    Roadmap P2.4). Best-effort: a webhook failure logs and never touches the
    session's own status."""
    url = settings.webhook_url
    if not url:
        return
    lines = [
        f"BetterTradingAgents autopilot session #{session_outcome.id} "
        f"{session_outcome.status} ({session_outcome.trigger})"
    ]
    if session_outcome.tickers:
        lines.append("Tickers: " + ", ".join(session_outcome.tickers))
    if session_outcome.decisions:
        lines.append(
            "Decisions: "
            + ", ".join(
                f"{ticker}={decision}"
                for ticker, decision in session_outcome.decisions.items()
            )
        )
    for order in session_outcome.orders:
        entry = f"Order: {order.side} {order.ticker}"
        if order.notional is not None:
            entry += f" ${order.notional:,.0f}"
        entry += f" -> {order.status}"
        if order.error:
            entry += f" ({order.error})"
        lines.append(entry)
    if session_outcome.error:
        lines.append(f"Error: {session_outcome.error}")
    try:
        client = get_client()
        await client.post(
            url,
            content="\n".join(lines),
            headers={"Content-Type": "text/plain"},
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        # The webhook URL itself is a secret; httpx errors can echo it whole.
        logger.warning("[automation] webhook POST failed: %s", redact(str(exc)))


async def run_session(trigger: str = "schedule") -> AutomationSession:
    """One full scan -> analyze -> order cycle. Never raises.

    The session lock is taken before any network call (P2.8), so a schedule
    tick racing a manual run sees it held instead of starting a parallel
    session; everything after the gate is in _run_session_locked.
    """
    if _session_lock.locked():
        return await _skip(trigger, "another session is already running")
    async with _session_lock:
        return await _run_session_locked(trigger)


async def _run_session_locked(trigger: str) -> AutomationSession:
    started_at = time.time()
    if not settings.alpaca_configured:
        return await _skip(trigger, "Alpaca paper trading is not configured", started_at)
    if not settings.alpaca_trading_enabled:
        return await _skip(trigger, "order submissions are disabled (kill switch)", started_at)
    if not settings.llm_configured:
        return await _skip(trigger, "no LLM configured; mock decisions never trade", started_at)

    from app import broker

    try:
        clock = await broker.clock()
    except Exception as exc:  # noqa: BLE001 - a broker failure must not kill the loop
        return await _skip(trigger, f"market clock unavailable: {exc}", started_at)
    if trigger == "schedule" and not clock["is_open"]:
        return await _skip(
            trigger, f"market closed until {clock.get('next_open') or 'the next open'}",
            started_at,
        )

    try:
        held = await broker.positions()
    except Exception as exc:  # noqa: BLE001
        return await _skip(trigger, f"paper positions unavailable: {exc}", started_at)
    held_map = {position.symbol: position for position in held}

    candidates: list[str] = []
    discovery_note = ""
    # Rotation: a deterministic screen would hand the loop the same top names
    # every session for weeks. Names analyzed in the cooldown window are held
    # back, so the shortlist moves down the ranking (backfilled only if the
    # fresh pool runs short). Held tickers are added regardless, further down.
    exclude: list[str] = []
    if settings.automation_candidate_cooldown_days > 0:
        cutoff = started_at - settings.automation_candidate_cooldown_days * 86400
        exclude = await asyncio.to_thread(_recent_candidate_tickers, cutoff)
    try:
        from app.discovery import discover_stocks

        discovery = await discover_stocks(
            normalize_outlook(settings.automation_outlook),
            settings.automation_candidates,
            exclude=exclude,
        )
        candidates = [t for t in discovery["tickers"] if t not in held_map]
        if exclude:
            logger.info(
                "[automation] rotation: %d recent ticker(s) held back "
                "(cooldown %d day(s))",
                len(exclude),
                settings.automation_candidate_cooldown_days,
            )
    except Exception as exc:  # noqa: BLE001 - fall back to a held-only session
        discovery_note = f"discovery failed: {exc}"

    # Held positions come first so the cost cap can only ever drop rotating
    # candidates, never an open position that still needs its sell-or-hold
    # decision; discovery failing entirely still leaves a held-only session.
    tickers = list(dict.fromkeys(list(held_map) + candidates))[:MAX_SESSION_TICKERS]
    if not tickers:
        return await _skip(
            trigger, discovery_note or "no candidates and no held positions", started_at
        )
    if discovery_note:
        logger.warning(
            "[automation] %s; proceeding with held tickers only", discovery_note
        )

    from app.runs import store

    session_id = await asyncio.to_thread(_insert_session, trigger, started_at)
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
                # One SPY read per session decides the bear-market brake for
                # every BUY below (None = unknown, brake fails open).
                bear = (
                    await _bear_market()
                    if any(
                        not a.error and a.decision == "BUY" for a in results.values()
                    )
                    else None
                )
                if bear:
                    logger.info(
                        "[automation] bear-market brake on: SPY below its 200-day average"
                    )
                for ticker, analysis in results.items():
                    if analysis.error:
                        continue
                    order = await _act_on_decision(
                        run.run_id, ticker, analysis, held_map, bear=bear
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
        await asyncio.to_thread(
            _finish_session, session_id, status, run_id, tickers, decisions, orders, error
        )
    finished = AutomationSession(
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
    if status in ("completed", "failed"):
        await _post_webhook(finished)
    return finished


async def _act_on_decision(
    run_id: str,
    ticker: str,
    analysis: StockAnalysis,
    held_map: dict,
    bear: bool | None = None,
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
        # held_map is the session-start snapshot; positions can change while
        # the analysis runs (a manual trade, an earlier exit this session), so
        # every BUY re-checks against the broker right before ordering (P2.8).
        try:
            held_now = {p.symbol for p in await broker.positions()}
        except Exception as exc:  # noqa: BLE001 - fail closed: skip, never double-buy
            logger.warning("[automation] %s position re-check failed: %s", ticker, exc)
            return AutomationOrderOutcome(
                ticker=ticker, side="buy", status="skipped",
                error="could not re-check positions before ordering",
            )
        if ticker in held_map or ticker in held_now:
            logger.info("[automation] %s already held; never adding to a position", ticker)
            return None
        notional = analysis.suggested_size_usd or settings.default_position_size
        side = "buy"
        if bear:  # wiki Roadmap P2.4: research and exits continue, BUYs brake
            return AutomationOrderOutcome(
                ticker=ticker, side="buy", notional=notional, status="braked",
                error="bear-market brake: SPY sits below its 200-day average",
            )
        # Portfolio budget (wiki Roadmap P2.4): deterministic, same shape as
        # every other limit - a share-of-equity deployment cap plus a cash
        # floor. Defaults (1.0 / 0.0) leave only the global risk gate.
        try:
            account = await broker.account()
        except Exception as exc:  # noqa: BLE001 - fail closed: skip, never over-deploy
            logger.warning("[automation] %s budget check failed: %s", ticker, exc)
            return AutomationOrderOutcome(
                ticker=ticker, side="buy", notional=notional, status="skipped",
                error="could not read the paper account before ordering",
            )
        if account.equity:
            deployed = await asyncio.to_thread(_automation_deployed)
            if deployed + notional > settings.automation_max_deployed_pct * account.equity:
                return AutomationOrderOutcome(
                    ticker=ticker, side="buy", notional=notional, status="skipped",
                    error=(
                        f"autopilot budget: ${deployed + notional:,.0f} deployed would "
                        f"pass {settings.automation_max_deployed_pct:.0%} of equity"
                    ),
                )
            if account.cash is not None and (
                account.cash - notional
                < settings.automation_cash_floor_pct * account.equity
            ):
                return AutomationOrderOutcome(
                    ticker=ticker, side="buy", notional=notional, status="skipped",
                    error=(
                        f"autopilot cash floor: would leave "
                        f"${max(0.0, account.cash - notional):,.0f}, below "
                        f"{settings.automation_cash_floor_pct:.0%} of equity"
                    ),
                )
        # Shares, not dollars: the budget stays a USD estimate, but the order
        # itself is a share count fixed at the decision's reference price.
        if not analysis.price or analysis.price <= 0:
            return AutomationOrderOutcome(
                ticker=ticker, side="buy", notional=notional, status="skipped",
                error="decision has no reference price to size shares from",
            )
        qty = broker.floor_qty(notional / analysis.price)
    elif analysis.decision == "SELL" and settings.automation_allow_sells:
        position = held_map.get(ticker)
        if position is None:
            return None  # the broker would reject a short; nothing to exit
        # Full exit in shares: selling the USD value lets the broker's own
        # price conversion overshoot the holding (the CMBT rejection).
        sellable = (
            position.available
            if position.available is not None
            else position.quantity
        )
        qty = broker.floor_qty(sellable)
        if qty <= 0:
            return AutomationOrderOutcome(
                ticker=ticker, side="sell", status="skipped",
                error="no sellable shares in the held position",
            )
        notional = round(qty * (analysis.price or position.current_price or 0.0), 2)
        side = "sell"
    else:
        return None

    try:
        order = await broker.submit_order(run_id, ticker, side, qty)
    except Exception as exc:  # noqa: BLE001 - one rejected order must not kill the session
        logger.warning("[automation] %s %s rejected: %s", side, ticker, exc)
        return AutomationOrderOutcome(
            ticker=ticker, side=side, notional=notional, status="rejected",
            error=str(exc)[:300],
        )
    logger.info(
        "[automation] %s %s %g sh (~$%.2f) -> %s",
        side, ticker, qty, notional, order.status,
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
            if await enabled() and not _session_lock.locked():
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
    if _session_lock.locked():
        return False
    _session_task = asyncio.create_task(run_session("manual"), name="automation-manual")
    return True
