"""Decision memory with realized-return reflection (wiki Roadmap 1.1).

Every completed run appends its decision to the `decisions` table. Before the
next run of the same ticker, past decisions are graded against realized prices
(own return and alpha vs SPY over MEMORY_HORIZON_DAYS); a decision that has
reached the horizon is graded once and the outcome stored back on its row.
Younger decisions get a partial, on-the-fly grade. The most recent few
reflections plus a couple of cross-ticker lessons are injected into the
Portfolio Manager dossier and shown on the result card.

Reflections are a deterministic sentence by default; `MEMORY_REFLECT_WITH_LLM=1`
asks a one-shot crew for a 2-sentence lesson instead (mature rows only, so the
extra cost is paid at most once per decision).

Since P1.17 the lazy per-ticker grading is no longer the only path: a
scheduled `settle_pending()` pass grades every decision whose horizon window
has closed, whether or not its ticker is analyzed again, so the scorecard,
the calibration, and the cross-ticker lessons fill in on their own.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from app.config import settings
from app.models import StockAnalysis
from app.db import connect as _connect

logger = logging.getLogger("memory")

MAX_TICKER_REFLECTIONS = 3
MAX_CROSS_LESSONS = 2
ALPHA_EDGE_PCT = 1.0  # |alpha| beyond this counts as a right/wrong call
MOVE_EDGE_PCT = 2.0  # |return| beyond this makes a HOLD an avoid/miss




def _init_db() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL DEFAULT '',
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                decision TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                price_at_decision REAL NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '',
                bull_case TEXT NOT NULL DEFAULT '',
                bear_case TEXT NOT NULL DEFAULT '',
                outcome_date TEXT,
                realized_return_pct REAL,
                spy_return_pct REAL,
                alpha_vs_spy_pct REAL,
                window_days REAL,
                horizon_days REAL,
                mature INTEGER NOT NULL DEFAULT 0,
                reflection TEXT
            )
            """
        )
        # Databases created before calibration provenance (ROADMAP P1.1) lack
        # these columns; their rows keep the defaults and group as "unknown /
        # unversioned" scope in the calibration report.
        for column in (
            "outlook TEXT NOT NULL DEFAULT ''",
            "depth TEXT NOT NULL DEFAULT ''",
            "model TEXT NOT NULL DEFAULT ''",
            "manager_decision TEXT",
            "manager_confidence REAL",
            "manager_probability REAL",
            "policy_version TEXT NOT NULL DEFAULT ''",
            "success_event TEXT NOT NULL DEFAULT ''",
            "horizon_days REAL",
        ):
            try:
                connection.execute(f"ALTER TABLE decisions ADD COLUMN {column}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise  # anything else is a real migration failure
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_decisions_ticker_date "
            "ON decisions(ticker, date DESC, id DESC)"
        )


async def init() -> None:
    await asyncio.to_thread(_init_db)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _insert_decision(
    run_id: str,
    analysis: StockAnalysis,
    decision_date: str,
    provenance: dict,
) -> int:
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO decisions (
                run_id, ticker, date, decision, confidence, price_at_decision,
                summary, bull_case, bear_case, outlook, depth, model,
                manager_decision, manager_confidence, manager_probability,
                policy_version, success_event, horizon_days
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                analysis.ticker,
                decision_date,
                analysis.decision,
                analysis.confidence,
                analysis.price or 0.0,
                analysis.summary[:600],
                analysis.bull_case[:400],
                analysis.bear_case[:400],
                provenance.get("outlook", ""),
                provenance.get("depth", ""),
                provenance.get("model", ""),
                analysis.manager_decision,
                analysis.manager_confidence,
                analysis.manager_probability,
                provenance.get("policy_version", ""),
                provenance.get("success_event", ""),
                provenance.get("horizon_days"),
            ),
        )
        return int(cursor.lastrowid)


async def record_decision(
    run_id: str,
    analysis: StockAnalysis,
    decision_date: str | None = None,
    provenance: dict | None = None,
) -> int:
    """Append one completed decision. `decision_date` defaults to today (UTC).

    An explicit date lets the check script (and later the backtester) seed
    decisions as of a past day so outcomes are gradeable. `provenance` carries
    the calibration scope (outlook, depth, model, policy version) so outcomes
    from materially different systems are never pooled silently (P1.1).
    """
    decision_date = decision_date or datetime.now(timezone.utc).date().isoformat()
    row_id = await asyncio.to_thread(
        _insert_decision, run_id, analysis, decision_date, provenance or {}
    )
    logger.info(
        "[memory] recorded %s %s %.0f%% @ %.2f",
        analysis.ticker,
        analysis.decision,
        analysis.confidence * 100,
        analysis.price or 0.0,
    )
    return row_id


# ---------------------------------------------------------------------------
# Outcome grading
# ---------------------------------------------------------------------------


async def _fetch_closes_pair(ticker: str, start: str) -> tuple[dict[str, float], dict[str, float]]:
    """Closes for the ticker and SPY from `start` through today (two downloads)."""
    from app.tools.market_data import get_closes_between

    end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()

    async def fetch(symbol: str) -> dict[str, float]:
        try:
            return await get_closes_between(symbol, start, end)
        except Exception as exc:  # noqa: BLE001 - grading must degrade, not fail
            logger.warning("[memory] %s close history failed: %s", symbol, exc)
            return {}

    own, spy = await asyncio.gather(fetch(ticker), fetch("SPY"))
    return own, spy


def close_on_or_before(closes: dict[str, float], day: str) -> tuple[str, float] | None:
    """The latest close at or before `day`. Shared with backtest grading."""
    candidates = [d for d in closes if d <= day]
    if not candidates:
        return None
    best = max(candidates)
    return best, closes[best]


def close_on_or_after(closes: dict[str, float], day: str) -> tuple[str, float] | None:
    """The earliest close at or after `day`. Shared with backtest grading."""
    candidates = [d for d in closes if d >= day]
    if not candidates:
        return None
    best = min(candidates)
    return best, closes[best]


def compute_outcome(
    row: dict, closes: dict[str, float], spy_closes: dict[str, float]
) -> dict | None:
    """Grade one decision against realized closes, using the same entry/exit
    convention as the backtest harness: entry is the last close on/before the
    decision date, exit is the FIRST close on/after the horizon target, so a
    target on a weekend or holiday walks forward to the next close and short
    horizons can always mature. Decisions whose window has not produced an
    exit close yet get a partial grade at the latest close (mature=False) for
    the reflections; None only when nothing can be graded at all.
    """
    try:
        decided = date.fromisoformat(row["date"])
    except ValueError:
        return None
    # The stored per-run horizon (from the outlook) wins; rows written before
    # per-outlook horizons fall back to the configured default.
    try:
        stored_horizon = row["horizon_days"]
    except (KeyError, IndexError):
        stored_horizon = None
    horizon = int(stored_horizon) if stored_horizon else settings.memory_horizon_days
    target = (decided + timedelta(days=horizon)).isoformat()

    entry = close_on_or_before(closes, row["date"])
    if entry is None:
        return None
    entry_day, entry_price = entry
    exit_point = close_on_or_after(closes, target)
    mature = exit_point is not None
    if mature:
        exit_day = exit_point[0]
    else:
        later = [d for d in closes if d > entry_day]
        if not later:
            return None  # decided on the last close - nothing to grade yet
        exit_day = max(later)
    exit_price = closes[exit_day]
    realized = (exit_price / entry_price - 1) * 100
    spy_entry = close_on_or_before(spy_closes, entry_day)
    spy_exit = close_on_or_before(spy_closes, exit_day)
    spy_return = (
        (spy_exit[1] / spy_entry[1] - 1) * 100
        if spy_entry and spy_exit and spy_exit[0] >= spy_entry[0] and spy_entry[1]
        else None
    )
    return {
        "outcome_date": exit_day,
        "realized_return_pct": round(realized, 2),
        "spy_return_pct": None if spy_return is None else round(spy_return, 2),
        "alpha_vs_spy_pct": None
        if spy_return is None
        else round(realized - spy_return, 2),
        "window_days": (date.fromisoformat(exit_day) - decided).days,
        "mature": mature,
    }


def lesson(decision: str, realized: float, alpha: float | None) -> str:
    """One-line deterministic verdict for a graded call."""
    if alpha is None:
        return "outcome could not be compared to SPY."
    if decision == "BUY":
        if alpha >= ALPHA_EDGE_PCT:
            return "the bullish call beat the market."
        if alpha <= -ALPHA_EDGE_PCT:
            return "the bullish call lagged the market."
        return "the bullish call roughly matched the market."
    if decision == "SELL":
        if alpha <= -ALPHA_EDGE_PCT:
            return "the bearish call was right - it fell harder than the market."
        if alpha >= ALPHA_EDGE_PCT:
            return "the bearish call was wrong - it outperformed the market."
        return "the bearish call roughly matched the market."
    if realized <= -MOVE_EDGE_PCT:
        return f"standing aside avoided a {realized:.1f}% slide."
    if realized >= MOVE_EDGE_PCT:
        return f"standing aside missed a {realized:.1f}% gain."
    return "standing aside cost little."


def verdict(decision: str, realized: float, alpha: float | None) -> str:
    """Right/wrong/neutral call per the edges above; one shared rule for the
    reflections, the benchmark scorecard, and the Accuracy page."""
    if decision == "BUY":
        if alpha is None:
            return "unknown"
        if alpha >= ALPHA_EDGE_PCT:
            return "right"
        if alpha <= -ALPHA_EDGE_PCT:
            return "wrong"
        return "neutral"
    if decision == "SELL":
        if alpha is None:
            return "unknown"
        if alpha <= -ALPHA_EDGE_PCT:
            return "right"
        if alpha >= ALPHA_EDGE_PCT:
            return "wrong"
        return "neutral"
    if realized <= -MOVE_EDGE_PCT:
        return "right"  # avoided a slide
    if realized >= MOVE_EDGE_PCT:
        return "wrong"  # missed a gain
    return "neutral"


def deterministic_reflection(row: dict, outcome: dict | None) -> str:
    decision = str(row["decision"])
    if outcome is None:
        return f"{decision} on {row['date']} has no graded outcome yet."
    realized = outcome["realized_return_pct"]
    alpha = outcome["alpha_vs_spy_pct"]
    spy = outcome["spy_return_pct"]
    window = outcome["window_days"]
    head = (
        f"{decision} at ${row['price_at_decision']:.2f} on {row['date']} returned "
        f"{realized:+.1f}% over {window}d"
        + (
            f" (SPY {spy:+.1f}%, alpha {alpha:+.1f}%)"
            if spy is not None
            else " (SPY comparison unavailable)"
        )
        + ("" if outcome["mature"] else ", partial window")
    )
    return f"{head}: {lesson(decision, realized, alpha)}"


async def _llm_reflection(row: dict, outcome: dict) -> str | None:
    """Optional LLM-written lesson; deterministic text stays the fallback."""
    try:
        from agents import Agent, Runner

        from app.agents import agent_instructions
        from app.workflow import get_llm

        llm = get_llm("analysts")  # reflection lessons are analyst-grade work
        if llm is None:
            return None
        payload = json.dumps(
            {
                "ticker": row["ticker"],
                "decision": row["decision"],
                "confidence": row["confidence"],
                "price_at_decision": row["price_at_decision"],
                "summary_of_the_call": row["summary"],
                **outcome,
            },
            default=str,
        )
        agent = Agent(
            name="Trading Reflection Analyst",
            instructions=agent_instructions(
                role="Trading Reflection Analyst",
                goal="Turn one graded past decision into a short, honest lesson.",
                backstory=(
                    "You review a trading system's past calls. You are blunt about "
                    "mistakes, do not cherry-pick, and never generalize from one outcome."
                ),
            ),
            model=llm.model_obj,
            model_settings=llm.model_settings,
        )
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                f"""Past decision with its realized outcome:
{payload}

Write exactly 2 sentences: (1) what the call got right or wrong versus SPY,
(2) one concrete lesson for the next decision on this ticker. Plain text only.""",
            ),
            timeout=settings.llm_timeout_seconds,
        )
        text = str(result.final_output or "").strip()
        return text[:400] or None
    except Exception as exc:  # noqa: BLE001 - LLM reflection is best-effort
        logger.warning("[memory] LLM reflection failed: %s", exc)
        return None


def _store_outcome(row_id: int, outcome: dict, reflection: str) -> None:
    with _connect() as connection:
        connection.execute(
            """
            UPDATE decisions SET outcome_date = ?, realized_return_pct = ?,
                spy_return_pct = ?, alpha_vs_spy_pct = ?, window_days = ?,
                mature = 1, reflection = ?
            WHERE id = ?
            """,
            (
                outcome["outcome_date"],
                outcome["realized_return_pct"],
                outcome["spy_return_pct"],
                outcome["alpha_vs_spy_pct"],
                outcome["window_days"],
                reflection,
                row_id,
            ),
        )


def _reflection_row(row: dict, outcome: dict | None, reflection: str | None) -> dict:
    return {
        "ticker": row["ticker"],
        "date": row["date"],
        "decision": row["decision"],
        "confidence": row["confidence"],
        "price_at_decision": row["price_at_decision"],
        "summary": row["summary"],
        "realized_return_pct": None if outcome is None else outcome["realized_return_pct"],
        "spy_return_pct": None if outcome is None else outcome["spy_return_pct"],
        "alpha_vs_spy_pct": None if outcome is None else outcome["alpha_vs_spy_pct"],
        "window_days": None if outcome is None else outcome["window_days"],
        "horizon_days": row.get("horizon_days") or settings.memory_horizon_days,
        "mature": bool(outcome is not None and outcome["mature"]),
        "reflection": reflection
        or deterministic_reflection(row, outcome),
    }


def _stored_outcome(row: dict) -> dict | None:
    if not row["mature"] or row["realized_return_pct"] is None:
        return None
    return {
        "outcome_date": row["outcome_date"],
        "realized_return_pct": row["realized_return_pct"],
        "spy_return_pct": row["spy_return_pct"],
        "alpha_vs_spy_pct": row["alpha_vs_spy_pct"],
        "window_days": row["window_days"],
        "mature": True,
    }


def _select_recent(ticker: str, limit: int) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM decisions WHERE ticker = ? "
            "ORDER BY date DESC, id DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _select_all() -> list[dict]:
    """Every recorded decision, newest first; the Accuracy page reads this."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM decisions ORDER BY date DESC, id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def _select_cross_lessons(ticker: str, limit: int) -> list[dict]:
    """Graded decisions on other tickers - stored outcomes only, so this never
    triggers new price downloads."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM decisions WHERE ticker != ? AND mature = 1 "
            "AND realized_return_pct IS NOT NULL "
            "ORDER BY date DESC, id DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
    return [dict(row) for row in rows]


async def get_reflections(ticker: str) -> dict:
    """Past decisions + lessons for the manager dossier.

    Returns {"past_decisions": [...up to 3 same-ticker reflections...],
             "cross_ticker_lessons": [...up to 2 graded lessons from other
             tickers...]}. Mature outcomes are computed once and stored;
    younger ones are re-graded cheaply from a single price fetch (only when
    the ticker has past decisions at all).
    """
    rows = await asyncio.to_thread(_select_recent, ticker, MAX_TICKER_REFLECTIONS)
    reflections: list[dict] = []
    if rows:
        stale = [
            row
            for row in rows
            if not (row["mature"] and row["realized_return_pct"] is not None)
        ]
        closes: dict[str, float] = {}
        spy_closes: dict[str, float] = {}
        if stale:
            start = min(row["date"] for row in stale)
            closes, spy_closes = await _fetch_closes_pair(ticker, start)
        for row in rows:
            stored = _stored_outcome(row)
            if stored is not None:
                reflections.append(_reflection_row(row, stored, row["reflection"]))
                continue
            outcome = compute_outcome(row, closes, spy_closes)
            if outcome is not None and outcome["mature"]:
                reflection = deterministic_reflection(row, outcome)
                if settings.memory_reflect_with_llm:
                    reflection = (
                        await _llm_reflection(row, outcome) or reflection
                    )
                await asyncio.to_thread(_store_outcome, row["id"], outcome, reflection)
                reflections.append(_reflection_row(row, outcome, reflection))
            else:
                # Younger decisions keep a deterministic text: their window is
                # still growing, so an LLM lesson would be re-paid every run.
                reflections.append(_reflection_row(row, outcome, None))

    cross_rows = await asyncio.to_thread(_select_cross_lessons, ticker, MAX_CROSS_LESSONS)
    cross = [_reflection_row(row, _stored_outcome(row), row["reflection"]) for row in cross_rows]
    return {"past_decisions": reflections, "cross_ticker_lessons": cross}


# ---------------------------------------------------------------------------
# Scheduled settling (ROADMAP P1.17)
# ---------------------------------------------------------------------------

_settle_task: asyncio.Task | None = None


def _select_unmatured() -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM decisions WHERE mature = 0 ORDER BY ticker, date, id"
        ).fetchall()
    return [dict(row) for row in rows]


def _is_due(row: dict, today: date) -> bool:
    """True when the horizon target is on or before `today`, so an exit close
    can exist. Shared by the settle pass and the Accuracy page's pending
    count; a row with an unreadable date is never due (and never graded)."""
    try:
        decided = date.fromisoformat(str(row["date"]))
    except (TypeError, ValueError):
        return False
    horizon = row["horizon_days"] or settings.memory_horizon_days
    return (decided + timedelta(days=int(horizon))) <= today


async def settle_pending() -> int:
    """Grade every decision whose horizon window has closed (P1.17).

    The lazy path in get_reflections only grades the ticker being analyzed,
    so a call on a ticker never analyzed again stayed unmatured forever and
    never reached the track record or the calibration. This pass settles all
    of them; grading rules stay in compute_outcome/verdict, this only changes
    when they run. Returns how many rows matured.
    """
    rows = await asyncio.to_thread(_select_unmatured)
    today = datetime.now(timezone.utc).date()
    due: dict[str, list[dict]] = {}
    for row in rows:
        if _is_due(row, today):
            due.setdefault(row["ticker"], []).append(row)
    settled = 0
    for ticker, ticker_rows in due.items():
        start = min(row["date"] for row in ticker_rows)
        closes, spy_closes = await _fetch_closes_pair(ticker, start)
        for row in ticker_rows:
            outcome = compute_outcome(row, closes, spy_closes)
            if outcome is not None and outcome["mature"]:
                await asyncio.to_thread(
                    _store_outcome,
                    row["id"],
                    outcome,
                    deterministic_reflection(row, outcome),
                )
                settled += 1
    if settled:
        logger.info("[memory] settled %d matured decision(s)", settled)
    return settled


async def _settle_loop() -> None:
    # Sleep-first: the first pass waits a full interval, so short-lived
    # processes (the offline checks) never trigger the price downloads.
    while True:
        await asyncio.sleep(max(15, settings.memory_settle_interval_minutes) * 60)
        try:
            await settle_pending()
        except Exception as exc:  # noqa: BLE001 - the loop outlives any bug
            logger.warning("[memory] settle pass failed: %s", exc)


def start() -> None:
    """Spawn the settle loop (idempotent; called from app startup)."""
    global _settle_task
    if _settle_task is None or _settle_task.done():
        _settle_task = asyncio.create_task(_settle_loop(), name="memory-settle")
