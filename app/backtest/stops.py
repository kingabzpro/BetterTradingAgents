"""Stop-overlay replay: what the live stops would have done, per decision.

The risk overlay (app/overlay.py) only runs live, so it accumulates evidence
in calendar time. This module replays its two mechanical rules over the
windows the backtest already grades - daily closes from entry to horizon -
and reports what differed: exits taken early by the absolute stop or the
trailing stop, and the net-return delta versus holding to the horizon.

Honest limits, stated once: daily closes only (an intraday trigger is read
at the daily close: conservative across gaps, optimistic between bars), the
cooldown and re-entry rules are not simulated (they need a portfolio, not a
decision), and the same round-trip cost is charged on both paths. The rule
itself is imported from app.overlay.evaluate, so the replay cannot drift
from what live trading enforces.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import fmean

from app.backtest.grade import Outcome
from app.memory import close_on_or_after, close_on_or_before


@dataclass
class StopReplay:
    fired: bool
    trigger: str  # "absolute_stop" | "trailing_stop" | ""
    exit_pct: float  # net return of the stopped path
    horizon_pct: float  # net return of the held-to-horizon path
    delta_pct: float  # exit minus horizon (negative = the stop cost money)


def replay_stops(
    closes: dict[str, float],
    decision_date: str,
    horizon_days: int,
    stop_pct: float,
    trail_pct: float,
    cost_pct: float,
) -> StopReplay | None:
    """One BUY window, replayed bar by bar; None while the window is open."""
    from app.overlay import evaluate

    entry_point = close_on_or_before(closes, decision_date)
    target = (date.fromisoformat(decision_date) + timedelta(days=horizon_days)).isoformat()
    exit_point = close_on_or_after(closes, target)
    if entry_point is None or exit_point is None:
        return None
    entry_day, entry_price = entry_point
    exit_day, exit_price = exit_point
    if entry_price <= 0:
        return None
    horizon_pct = round((exit_price / entry_price - 1) * 100 - cost_pct, 2)

    peak = entry_price
    for day in sorted(closes):
        if day <= entry_day or day > exit_day:
            continue  # walk only the held window
        price = closes[day]
        if price <= 0:
            continue
        trigger = evaluate(entry_price, peak, price, stop_pct, trail_pct)
        peak = max(peak, price)
        if trigger:
            exit_pct = round((price / entry_price - 1) * 100 - cost_pct, 2)
            return StopReplay(True, trigger, exit_pct, horizon_pct, round(exit_pct - horizon_pct, 2))
    return StopReplay(False, "", horizon_pct, horizon_pct, 0.0)


def stop_replay_summary(
    outcomes: list[Outcome],
    closes_by_ticker: dict[str, dict[str, float]],
    stop_pct: float,
    trail_pct: float,
) -> dict | None:
    """Aggregate the replay across every graded BUY in the sample."""
    results = []
    for outcome in outcomes:
        if outcome.decision != "BUY":
            continue
        replay = replay_stops(
            closes_by_ticker.get(outcome.ticker, {}),
            outcome.date,
            outcome.window_days,
            stop_pct,
            trail_pct,
            outcome.cost_pct,
        )
        if replay is not None:
            results.append(replay)
    if not results:
        return None
    fired = [r for r in results if r.fired]
    return {
        "buy_decisions": len(results),
        "stopped": len(fired),
        "absolute_stops": sum(1 for r in fired if r.trigger == "absolute_stop"),
        "trailing_stops": sum(1 for r in fired if r.trigger == "trailing_stop"),
        "stop_loss_pct": round(stop_pct * 100, 1),
        "trailing_stop_pct": round(trail_pct * 100, 1),
        "mean_delta_pct": round(fmean(r.delta_pct for r in results), 2),
        "sum_delta_pct": round(sum(r.delta_pct for r in results), 2),
    }
