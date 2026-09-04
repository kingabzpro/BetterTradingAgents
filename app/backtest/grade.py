"""Decision grading and aggregate metrics (ROADMAP 2.2) - pure functions.

A BUY graded over the horizon window earns the stock's return minus the
round-trip cost; a SELL earns the inverse when shorting is enabled, otherwise
0 (long-only mode); a HOLD earns 0. Alpha compares the net decision return
with SPY over the same window. Entry is the last close on/before the decision
date, exit the first close on/after decision date + horizon days - windows
without an exit close (too recent) are ungraded and excluded from aggregates.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import fmean, pstdev

ROUND_TRIP_COST_PCT = 0.10  # 2 x 5bp, per positioned decision


@dataclass
class Decision:
    ticker: str
    date: str
    decision: str
    confidence: float


@dataclass
class Outcome:
    ticker: str
    date: str
    decision: str
    confidence: float
    entry: float
    exit: float
    window_days: int
    gross_pct: float  # the stock's own move over the window
    net_pct: float  # what the decision earned after costs
    spy_pct: float | None
    alpha_pct: float | None
    note: str = ""


def close_on_or_before(closes: dict[str, float], day: str) -> tuple[str, float] | None:
    candidates = [d for d in closes if d <= day]
    if not candidates:
        return None
    best = max(candidates)
    return best, closes[best]


def close_on_or_after(closes: dict[str, float], day: str) -> tuple[str, float] | None:
    candidates = [d for d in closes if d >= day]
    if not candidates:
        return None
    best = min(candidates)
    return best, closes[best]


def grade(
    decision: Decision,
    closes: dict[str, float],
    spy_closes: dict[str, float],
    horizon_days: int,
    cost_pct: float = ROUND_TRIP_COST_PCT,
    short: bool = False,
) -> Outcome | None:
    """Grade one decision; None while the window has no exit close yet."""
    entry = close_on_or_before(closes, decision.date)
    target = (date.fromisoformat(decision.date) + timedelta(days=horizon_days)).isoformat()
    exit_point = close_on_or_after(closes, target)
    if entry is None or exit_point is None:
        return None
    entry_day, entry_price = entry
    exit_day, exit_price = exit_point

    gross = (exit_price / entry_price - 1) * 100
    note = ""
    if decision.decision == "BUY":
        net = gross - cost_pct
    elif decision.decision == "SELL":
        if short:
            net = -gross - cost_pct
        else:
            net, note = 0.0, "long-only mode: SELL scores 0"
    else:
        net, note = 0.0, "HOLD scores 0"

    spy_entry = close_on_or_before(spy_closes, entry_day)
    spy_exit = close_on_or_after(spy_closes, exit_day)
    spy_pct = None
    if (
        spy_entry is not None
        and spy_exit is not None
        and spy_entry[0] <= spy_exit[0]
        and spy_entry[1]
    ):
        spy_pct = (spy_exit[1] / spy_entry[1] - 1) * 100

    positioned = decision.decision == "BUY" or (decision.decision == "SELL" and short)
    alpha = (net - spy_pct) if (spy_pct is not None and positioned) else None

    return Outcome(
        ticker=decision.ticker,
        date=decision.date,
        decision=decision.decision,
        confidence=decision.confidence,
        entry=round(entry_price, 4),
        exit=round(exit_price, 4),
        window_days=(date.fromisoformat(exit_day) - date.fromisoformat(decision.date)).days,
        gross_pct=round(gross, 2),
        net_pct=round(net, 2),
        spy_pct=None if spy_pct is None else round(spy_pct, 2),
        alpha_pct=None if alpha is None else round(alpha, 2),
        note=note,
    )


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def cumulative_return_pct(nets: list[float]) -> float:
    equity = 1.0
    for net in nets:
        equity *= 1 + net / 100
    return round((equity - 1) * 100, 2)


def max_drawdown_pct(nets: list[float]) -> float:
    """Largest peak-to-trough drop of the compounded equity curve, in %."""
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for net in nets:
        equity *= 1 + net / 100
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak if peak else 0.0)
    return round(drawdown * 100, 2)


def sharpe(nets: list[float], periods_per_year: float) -> float:
    """Annualized Sharpe of the per-decision returns (0 without variance)."""
    if len(nets) < 2:
        return 0.0
    std = pstdev(nets)
    if std == 0:
        return 0.0
    return round(fmean(nets) / std * (periods_per_year**0.5), 2)


def aggregate(outcomes: list[Outcome], step_days: int) -> dict:
    """Per-run metrics: hit rate, averages, cumulative, Sharpe, drawdown."""
    nets = [o.net_pct for o in outcomes]
    positioned = [
        o for o in outcomes if o.decision == "BUY" or (o.decision == "SELL" and o.note == "")
    ]
    wins = [o for o in positioned if o.net_pct > 0]
    alphas = [o.alpha_pct for o in outcomes if o.alpha_pct is not None]
    periods_per_year = 365.0 / max(1, step_days)
    return {
        "decisions": len(outcomes),
        "counts": {
            kind: sum(1 for o in outcomes if o.decision == kind)
            for kind in ("BUY", "SELL", "HOLD")
        },
        "hit_rate_pct": round(len(wins) / len(positioned) * 100, 1) if positioned else None,
        "avg_net_pct": round(fmean(nets), 2) if nets else None,
        "avg_alpha_pct": round(fmean(alphas), 2) if alphas else None,
        "cumulative_pct": cumulative_return_pct(nets),
        "sharpe": sharpe(nets, periods_per_year),
        "max_drawdown_pct": max_drawdown_pct(nets),
        "periods_per_year": round(periods_per_year, 1),
    }


def buy_hold_pct(closes: dict[str, float], outcomes: list[Outcome]) -> float | None:
    """Buy-and-hold of the ticker across the graded span (first entry to last exit)."""
    if not outcomes:
        return None
    entry = close_on_or_before(closes, outcomes[0].date)
    last = max(outcomes, key=lambda o: o.date)
    exit_day = (
        date.fromisoformat(last.date) + timedelta(days=last.window_days)
    ).isoformat()
    exit_point = close_on_or_after(closes, exit_day)
    if entry is None or exit_point is None or not entry[1]:
        return None
    return round((exit_point[1] / entry[1] - 1) * 100, 2)


@dataclass
class BacktestResult:
    config: dict
    flags: dict
    tickers: dict[str, dict] = field(default_factory=dict)
    overall: dict = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)
    ungraded: list[Decision] = field(default_factory=list)
