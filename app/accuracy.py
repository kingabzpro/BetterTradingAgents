"""The Accuracy page's data: every past call vs what the market did (P1.9).

Reads the decisions table the memory system already grades. Rows whose window
closed but were never graded (the ticker was not analyzed again, so nothing
triggered the lazy grading) are graded here once and stored back through the
same helpers, so the page and the benchmark scorecard cannot drift apart.
No new math: outcomes come from memory.compute_outcome, verdicts from
memory.verdict.

Repeated analyses of the same call (reruns, autopilot sessions) count once
per (ticker, date, decision), the same dedupe rule as the benchmark
scorecard; without it a re-run multiplies one call's outcome in every rate.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone

from app import memory
from app.config import settings

SLIDE_PCT = -10.0  # a HOLD that stood aside through this counts as a "slide avoided"


def _lesson(row: dict) -> str:
    if row.get("reflection"):
        return row["reflection"]
    outcome = {
        "realized_return_pct": row["realized_return_pct"],
        "spy_return_pct": row["spy_return_pct"],
        "alpha_vs_spy_pct": row["alpha_vs_spy_pct"],
        "window_days": row["window_days"],
        "mature": True,
    }
    return memory.deterministic_reflection(row, outcome)


def _row_payload(row: dict) -> dict:
    return {
        "ticker": row["ticker"],
        "date": row["date"],
        "decision": row["decision"],
        "confidence": row["confidence"] or 0.0,
        "entry_price": row["price_at_decision"] or 0.0,
        "realized_return_pct": row["realized_return_pct"],
        "spy_return_pct": row["spy_return_pct"],
        "alpha_vs_spy_pct": row["alpha_vs_spy_pct"],
        "window_days": row["window_days"],
        "verdict": memory.verdict(
            str(row["decision"]),
            row["realized_return_pct"] or 0.0,
            row["alpha_vs_spy_pct"],
        ),
        "lesson": _lesson(row),
    }


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _per_signal(rows: list[dict]) -> dict:
    """Mean outcome per $10k signal: following the calls (long-only: BUYs ride
    the window, HOLD/SELL stay in cash) vs buying every ticker vs SPY."""
    realized = [row["realized_return_pct"] for row in rows if row["realized_return_pct"] is not None]
    followed = [
        row["realized_return_pct"]
        for row in rows
        if row["decision"] == "BUY" and row["realized_return_pct"] is not None
    ]
    spy = [row["spy_return_pct"] for row in rows if row["spy_return_pct"] is not None]
    return {
        "n": len(rows),
        "follow_calls_pct": _mean([*followed, *[0.0] * (len(realized) - len(followed))]),
        "always_buy_pct": _mean(realized),
        "spy_pct": _mean(spy),
    }


def _aggregates(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for decision in ("BUY", "HOLD", "SELL"):
        group = [row for row in rows if row["decision"] == decision]
        counts = {"right": 0, "wrong": 0, "neutral": 0}
        for row in group:
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        decisive = counts["right"] + counts["wrong"]
        alphas = [row["alpha_vs_spy_pct"] for row in group if row["alpha_vs_spy_pct"] is not None]
        realized = [row["realized_return_pct"] for row in group if row["realized_return_pct"] is not None]
        out.append(
            {
                "decision": decision,
                "n": len(group),
                "right": counts["right"],
                "wrong": counts["wrong"],
                "neutral": counts["neutral"],
                # Hit rate over decisive calls only; neutrals stay visible as
                # their own count so the denominator is never hidden.
                "hit_rate": (counts["right"] / decisive) if decisive else None,
                "mean_alpha_pct": _mean(alphas),
                "mean_realized_pct": _mean(realized),
            }
        )
    return out


def _avoided_slides(rows: list[dict]) -> list[dict]:
    """HOLDs that stood aside through a double-digit slide."""
    slides = [
        {"ticker": row["ticker"], "realized_pct": row["realized_return_pct"]}
        for row in rows
        if row["decision"] == "HOLD" and (row["realized_return_pct"] or 0.0) <= SLIDE_PCT
    ]
    return sorted(slides, key=lambda slide: slide["realized_pct"])


async def accuracy_report() -> dict:
    """Graded unique calls (newest first), aggregates, per-signal comparison,
    avoided slides, pending count."""
    all_rows = await asyncio.to_thread(memory._select_all)
    # One row per unique call: reruns and autopilot re-analyses of the same
    # ticker on the same day with the same decision are the same call.
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in all_rows:
        key = (row["ticker"], row["date"], row["decision"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    today = datetime.now(timezone.utc).date()
    graded: list[dict] = []
    pending = 0
    # Stale = no stored outcome yet. Rows whose window cannot have closed yet
    # are pending without any price download.
    stale: dict[str, list[dict]] = {}
    for row in rows:
        if row["mature"] and row["realized_return_pct"] is not None:
            graded.append(row)
            continue
        try:
            decided = date.fromisoformat(row["date"])
        except ValueError:
            pending += 1
            continue
        target = decided + timedelta(days=settings.memory_horizon_days)
        if target > today:
            pending += 1  # no close on/after the target can exist yet
            continue
        stale.setdefault(row["ticker"], []).append(row)

    for ticker, ticker_rows in stale.items():
        start = min(row["date"] for row in ticker_rows)
        closes, spy_closes = await memory._fetch_closes_pair(ticker, start)
        for row in ticker_rows:
            outcome = memory.compute_outcome(row, closes, spy_closes)
            if outcome is not None and outcome["mature"]:
                reflection = memory.deterministic_reflection(row, outcome)
                await asyncio.to_thread(memory._store_outcome, row["id"], outcome, reflection)
                row.update(
                    {
                        "realized_return_pct": outcome["realized_return_pct"],
                        "spy_return_pct": outcome["spy_return_pct"],
                        "alpha_vs_spy_pct": outcome["alpha_vs_spy_pct"],
                        "window_days": outcome["window_days"],
                    }
                )
                graded.append(row)
            else:
                # Window closed but no gradeable closes came back; stays
                # pending so the count shows the gap instead of hiding it.
                pending += 1

    graded.sort(key=lambda row: (row["date"], row["id"]), reverse=True)
    payloads = [_row_payload(row) for row in graded]
    return {
        "horizon_days": settings.memory_horizon_days,
        "graded": len(graded),
        "pending": pending,
        "rows": payloads,
        "by_decision": _aggregates(payloads),
        "per_signal": _per_signal(payloads),
        "avoided_slides": _avoided_slides(payloads),
    }
