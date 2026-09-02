"""Deterministic risk gate applied after the Portfolio Manager decides.

Rules, not an LLM: free, testable, no hallucination surface (docs/ROADMAP.md 1.2).

- Sizing: dollar-volatility parity scaled by conviction -
  size = DEFAULT_POSITION_SIZE x confidence x min(1, target_vol / realized_vol),
  so a 30%-vol name gets half the dollar size of a 15%-vol name.
- Exposure caps: a BUY is downgraded to HOLD when the sized position would
  breach the max single-position share of equity, the max invested share,
  or the minimum cash buffer.
- Missing-input brake: 2+ failed analysts cap confidence at 0.5.
"""

import logging

from app.config import settings
from app.models import AgentResult, PortfolioSummary

logger = logging.getLogger("risk")

VOL_TARGET_PCT = 15.0  # annualized volatility the default position size assumes
SIZE_FLOOR_MULT = 0.25
SIZE_CEIL_MULT = 1.5


def size_position(confidence: float, vol_ann_pct: float | None, default_size: float) -> float:
    """Dollar-vol parity position size scaled by conviction."""
    if vol_ann_pct is None or vol_ann_pct <= 0:
        vol_scale = 1.0
    else:
        vol_scale = min(1.0, VOL_TARGET_PCT / vol_ann_pct)
    mult = min(SIZE_CEIL_MULT, max(SIZE_FLOOR_MULT, confidence * vol_scale))
    return round(default_size * mult, 2)


def evaluate(
    decision: str,
    confidence: float,
    ticker: str,
    analysts: tuple[AgentResult | None, ...],
    vol_ann_pct: float | None,
    portfolio: PortfolioSummary | None,
) -> tuple[str, float, float | None, list[str]]:
    """Apply the risk gate to a manager decision.

    Returns (decision, confidence, suggested_size_usd, risk_flags).
    """
    flags: list[str] = []

    failed = sum(1 for a in analysts if a is None)
    if failed >= 2:
        if confidence > 0.5:
            confidence = 0.5
        flags.append(f"confidence capped at 50%: {failed}/3 analyst inputs failed")

    size_usd: float | None = None
    if decision == "BUY":
        size_usd = size_position(confidence, vol_ann_pct, settings.default_position_size)

        if portfolio is None:
            flags.append("exposure caps skipped: portfolio unavailable")
        elif not portfolio.total_equity:
            flags.append("exposure caps skipped: portfolio value unavailable")
        else:
            equity = portfolio.total_equity
            invested = portfolio.positions_value or 0.0
            held = sum(
                (p.value if p.value is not None else p.cost)
                for p in portfolio.positions
                if p.ticker == ticker
            )
            reasons = []
            if (held + size_usd) / equity > settings.max_position_pct + 1e-9:
                reasons.append(
                    f"{ticker} exposure would exceed "
                    f"{settings.max_position_pct:.0%} of equity"
                )
            if (invested + size_usd) / equity > settings.max_invested_pct + 1e-9:
                reasons.append(
                    f"invested capital would exceed "
                    f"{settings.max_invested_pct:.0%} of equity"
                )
            if portfolio.cash - size_usd < settings.min_cash_pct * equity - 1e-9:
                reasons.append(
                    f"cash buffer would fall below "
                    f"{settings.min_cash_pct:.0%} of equity"
                )
            if reasons:
                flags.append("downgraded BUY to HOLD: " + "; ".join(reasons))
                logger.info("[risk] %s BUY downgraded: %s", ticker, "; ".join(reasons))
                decision = "HOLD"
                size_usd = None

    return decision, confidence, size_usd, flags
