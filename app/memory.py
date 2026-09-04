"""Decision memory with realized-return reflection (docs/ROADMAP.md 1.1).

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
"""

import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from app.config import settings
from app.models import StockAnalysis

logger = logging.getLogger("memory")

MAX_TICKER_REFLECTIONS = 3
MAX_CROSS_LESSONS = 2
ALPHA_EDGE_PCT = 1.0  # |alpha| beyond this counts as a right/wrong call
MOVE_EDGE_PCT = 2.0  # |return| beyond this makes a HOLD an avoid/miss


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    return connection


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
                mature INTEGER NOT NULL DEFAULT 0,
                reflection TEXT
            )
            """
        )
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
) -> int:
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO decisions (
                run_id, ticker, date, decision, confidence, price_at_decision,
                summary, bull_case, bear_case
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        return int(cursor.lastrowid)


async def record_decision(
    run_id: str, analysis: StockAnalysis, decision_date: str | None = None
) -> int:
    """Append one completed decision. `decision_date` defaults to today (UTC).

    An explicit date lets the check script (and later the backtester) seed
    decisions as of a past day so outcomes are gradeable.
    """
    decision_date = decision_date or datetime.now(timezone.utc).date().isoformat()
    row_id = await asyncio.to_thread(_insert_decision, run_id, analysis, decision_date)
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


def _close_on_or_before(closes: dict[str, float], day: str) -> tuple[str, float] | None:
    candidates = [d for d in closes if d <= day]
    if not candidates:
        return None
    best = max(candidates)
    return best, closes[best]


def compute_outcome(
    row: dict, closes: dict[str, float], spy_closes: dict[str, float]
) -> dict | None:
    """Grade one decision against realized closes.

    Entry is the first close on/after the decision date; exit is the last close
    within the horizon (or the latest close if the horizon is not reached yet).
    Returns None while no close after the decision exists yet.
    """
    try:
        decided = date.fromisoformat(row["date"])
    except ValueError:
        return None
    horizon = settings.memory_horizon_days
    target = (decided + timedelta(days=horizon)).isoformat()

    dates = sorted(closes)
    entry = next(((d, closes[d]) for d in dates if d >= row["date"]), None)
    if entry is None:
        return None
    entry_day, entry_price = entry
    limit = min(target, dates[-1])
    exit_day = max((d for d in dates if entry_day < d <= limit), default=None)
    if exit_day is None:
        return None  # decided today (or on the last close) - nothing to grade yet

    exit_price = closes[exit_day]
    realized = (exit_price / entry_price - 1) * 100
    spy_entry = _close_on_or_before(spy_closes, entry_day)
    spy_exit = _close_on_or_before(spy_closes, exit_day)
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
        "mature": exit_day >= target,
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
    """Optional crew-written lesson; deterministic text stays the fallback."""
    try:
        from crewai import Agent, Crew, Task

        from app.workflow import get_llm

        llm = get_llm("analysts")  # reflection lessons are analyst-grade work
        if llm is None:
            return None
        agent = Agent(
            role="Trading Reflection Analyst",
            goal="Turn one graded past decision into a short, honest lesson.",
            backstory=(
                "You review a trading system's past calls. You are blunt about "
                "mistakes, do not cherry-pick, and never generalize from one outcome."
            ),
            llm=llm,
            allow_delegation=False,
        )
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
        task = Task(
            description=f"""Past decision with its realized outcome:
{payload}

Write exactly 2 sentences: (1) what the call got right or wrong versus SPY,
(2) one concrete lesson for the next decision on this ticker. Plain text only.""",
            expected_output="Two plain sentences, no JSON, no markdown.",
            agent=agent,
        )
        output = await asyncio.wait_for(
            Crew(agents=[agent], tasks=[task]).kickoff_async(),
            timeout=settings.llm_timeout_seconds,
        )
        text = str(getattr(output, "raw", "")).strip()
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
