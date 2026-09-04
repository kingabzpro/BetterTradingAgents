"""Deterministic risk gate applied after the Portfolio Manager decides.

Rules, not an LLM: free, testable, no hallucination surface (docs/ROADMAP.md 1.2).

- Sizing: dollar-volatility parity scaled by conviction -
  size = DEFAULT_POSITION_SIZE x confidence x min(1, target_vol / realized_vol),
  so a 30%-vol name gets half the dollar size of a 15%-vol name.
- Exposure caps: a BUY is downgraded to HOLD when the sized position would
  breach the max single-position share of equity, the max invested share,
  or the minimum cash buffer.
- Forecast check: the 5-day forecast is standardized against the stock's own
  volatility (z = forecast / (vol_ann x sqrt(5/252)), the same inverse-vol
  construction as Barroso & Santa-Clara 2015). Beyond -1 sigma a BUY is
  downgraded and a SELL vetoed; past -0.5 sigma the size is halved. Inside
  half a sigma the forecast is noise and does not touch the decision.
- Missing-input brake: 2+ failed analysts cap confidence at 0.5.
"""

import logging
from math import sqrt

from app.config import settings
from app.models import AgentResult, PortfolioSummary

logger = logging.getLogger("risk")

VOL_TARGET_PCT = 15.0  # annualized volatility the default position size assumes
SIZE_FLOOR_MULT = 0.25
SIZE_CEIL_MULT = 1.5
FORECAST_VETO_Z = 1.0  # |z| beyond this contradicts the decision
FORECAST_HALVE_Z = 0.5  # z past this (against the decision) halves the size
FORECAST_HORIZON_DAYS = 5


def forecast_noise_pct(vol_ann_pct: float | None) -> float | None:
    """The +/-1 sigma band for a 5-day move, in percent, from annualized vol."""
    if vol_ann_pct is None or vol_ann_pct <= 0:
        return None
    return vol_ann_pct * sqrt(FORECAST_HORIZON_DAYS / 252.0)


def forecast_z(forecast_change_pct: float | None, vol_ann_pct: float | None) -> float | None:
    """Forecast change as a multiple of its own 5-day noise band."""
    band = forecast_noise_pct(vol_ann_pct)
    if band is None or forecast_change_pct is None:
        return None
    return forecast_change_pct / band


def forecast_signal(z: float | None) -> str:
    """Plain-language reading of the forecast z-score."""
    if z is None:
        return "unavailable"
    if z <= -FORECAST_VETO_Z:
        return "clearly_bearish"
    if z <= -FORECAST_HALVE_Z:
        return "leans_bearish"
    if z < FORECAST_HALVE_Z:
        return "no_edge statistical noise"
    if z < FORECAST_VETO_Z:
        return "leans_bullish"
    return "clearly_bullish"


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
    forecast_change_pct: float | None = None,
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

    z = forecast_z(forecast_change_pct, vol_ann_pct)
    band = forecast_noise_pct(vol_ann_pct)

    if decision == "BUY":
        if z is not None and z <= -FORECAST_VETO_Z:
            flags.append(
                f"downgraded BUY to HOLD: 5-day forecast {forecast_change_pct:.2f}% "
                f"is beyond the +/-{band:.1f}% noise band (z={z:.2f})"
            )
            logger.info("[risk] %s BUY downgraded: forecast z=%.2f", ticker, z)
            return "HOLD", confidence, None, flags

        size_usd = size_position(confidence, vol_ann_pct, settings.default_position_size)

        if z is not None and z <= -FORECAST_HALVE_Z:
            size_usd = round(size_usd * 0.5, 2)
            flags.append(
                f"position size halved: 5-day forecast {forecast_change_pct:.2f}% "
                f"leans past half the noise band (z={z:.2f})"
            )

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

    if decision == "SELL" and z is not None and z >= FORECAST_VETO_Z:
        flags.append(
            f"downgraded SELL to HOLD: 5-day forecast {forecast_change_pct:.2f}% "
            f"is beyond the +/-{band:.1f}% noise band (z={z:.2f})"
        )
        logger.info("[risk] %s SELL downgraded: forecast z=%.2f", ticker, z)
        decision = "HOLD"

    return decision, confidence, None, flags
