"""Deterministic risk-regime gate (phase 4, item 18; pattern from FinRL-X).

The gate never picks assets; it scales the risk budget the existing gate
applies to NEW BUYs:

- slow regime (daily bars): SPY below its 26-week mean, a 13-week drawdown
  past 10%, and a VIX robust z-score at 3+ each add one stress point; 0 =
  risk_on (1.0x), 1 = neutral (0.75x), 2+ = risk_off (0.0x), and risk_off
  only sticks when the same score also held a week ago (persistence).
- fast overlay: a 3-session SPY shock past -4% or a VIX jump past +25%
  forces a 0.0x budget outright, because the slow gate is deliberately late.

Every input is the series up to the as-of date, so backtests replay the same
decision the live run would have made. Missing or too-short data fails to
risk_on with a note: a feed hiccup must never silently shrink positions.
"""

import logging
from dataclasses import dataclass
from statistics import median

logger = logging.getLogger("risk_regime")

MA_DAYS = 130  # 26 weeks of daily bars
DRAWDOWN_DAYS = 65  # 13 weeks
MIN_BARS = 140  # trend + drawdown need this much history to mean anything
VIX_Z_SPIKE = 3.0
DRAWDOWN_STRESS = 0.10
PERSISTENCE_BARS = 5  # risk_off must also have held a week ago
FAST_LOOKBACK = 3
FAST_SPY_SHOCK = -0.04
FAST_VIX_JUMP = 0.25
VIX_HISTORY_CAP = 756  # ~3 years of daily bars for the robust z-score


@dataclass
class RiskBudget:
    state: str  # risk_on | neutral | risk_off | fast_risk_off
    multiplier: float  # 1.0 / 0.75 / 0.0
    detail: str  # plain language; lands in risk flags when it binds


def robust_zscore(values: list[float]) -> float:
    """Median/MAD z-score: resistant to the fat tails VIX likes to grow."""
    if len(values) < 20:
        return 0.0
    med = median(values)
    deviations = sorted(abs(v - med) for v in values)
    mad = median(deviations)
    if mad <= 0:
        return 0.0
    return (values[-1] - med) / (1.4826 * mad)


def _score_at(spy: list[float], vix: list[float], index: int) -> tuple[int, list[str]]:
    """Stress score (0-3) and reasons for the bar at `index`."""
    reasons: list[str] = []
    score = 0
    window = spy[: index + 1]
    if len(window) < MA_DAYS:
        return 0, ["insufficient SPY history"]
    if window[-1] < sum(window[-MA_DAYS:]) / MA_DAYS:
        score += 1
        reasons.append("SPY below its 26-week mean")
    recent_high = max(window[-DRAWDOWN_DAYS:])
    if recent_high > 0 and (recent_high - window[-1]) / recent_high >= DRAWDOWN_STRESS:
        score += 1
        reasons.append(f"SPY {DRAWDOWN_STRESS:.0%}+ below its 13-week high")
    vix_window = vix[: index + 1][-VIX_HISTORY_CAP:]
    z = robust_zscore(vix_window)
    if z >= VIX_Z_SPIKE:
        score += 1
        reasons.append(f"VIX z-score {z:.1f} (spike)")
    return score, reasons


def risk_budget(spy: list[float], vix: list[float]) -> RiskBudget:
    """The budget for the latest bar of the series (see module docstring)."""
    if len(spy) < MIN_BARS:
        return RiskBudget("risk_on", 1.0, "regime inputs too short; gate open")

    if len(spy) > FAST_LOOKBACK:
        move = spy[-1] / spy[-1 - FAST_LOOKBACK] - 1
        vix_move = (
            vix[-1] / vix[-1 - FAST_LOOKBACK] - 1
            if len(vix) > FAST_LOOKBACK and vix[-1 - FAST_LOOKBACK] > 0
            else 0.0
        )
        if move <= FAST_SPY_SHOCK or vix_move >= FAST_VIX_JUMP:
            return RiskBudget(
                "fast_risk_off",
                0.0,
                f"fast risk-off: SPY {move:+.1%} / VIX {vix_move:+.0%} over "
                f"{FAST_LOOKBACK} sessions",
            )

    score, reasons = _score_at(spy, vix, len(spy) - 1)
    if score >= 2:
        prior_score, _ = _score_at(spy, vix, len(spy) - 1 - PERSISTENCE_BARS)
        if prior_score >= 2:
            return RiskBudget("risk_off", 0.0, "risk-off: " + "; ".join(reasons))
        return RiskBudget(
            "neutral", 0.75, "risk-off signals not yet persistent: " + "; ".join(reasons)
        )
    if score == 1:
        return RiskBudget("neutral", 0.75, "neutral: " + "; ".join(reasons))
    return RiskBudget("risk_on", 1.0, "risk-on")


async def fetch_regime_budget() -> RiskBudget:
    """Live budget from cached SPY/^VIX daily closes; best-effort.

    Uses the shared history TTL cache, so sibling tickers in one run re-read
    the same series without new provider calls. Any failure returns an open
    gate; the note says why.
    """
    from datetime import date, timedelta

    from app.tools.market_data import get_closes_between

    end = date.today().isoformat()
    start = (date.today() - timedelta(days=420)).isoformat()
    try:
        spy = await get_closes_between("SPY", start, end)
        vix = await get_closes_between("^VIX", start, end)
        spy_values = [v for _, v in sorted(spy.items())]
        vix_values = [v for _, v in sorted(vix.items())]
        if not spy_values:
            return RiskBudget("risk_on", 1.0, "SPY history unavailable; gate open")
        return risk_budget(spy_values, vix_values)
    except Exception as exc:  # noqa: BLE001 - the gate degrades to open
        logger.warning("[regime] inputs unavailable: %s", exc)
        return RiskBudget("risk_on", 1.0, "regime inputs unavailable; gate open")
