"""Deterministic risk gate applied after the Portfolio Manager decides.

Rules, not an LLM: free, testable, no hallucination surface (wiki Roadmap 1.2).

- Sizing: dollar-volatility parity scaled by conviction -
  size = DEFAULT_POSITION_SIZE x confidence x min(1, target_vol / realized_vol),
  so a 30%-vol name gets half the dollar size of a 15%-vol name.
- Exposure caps: a BUY is downgraded to HOLD when the sized position would
  breach the max single-position share of equity, the max invested share,
  or the minimum cash buffer.
- Drawdown brake: a BUY halts when open positions' unrealized loss exceeds
  MAX_DRAWDOWN_PCT of the cash-plus-cost basis behind them. Losses already
  realized through SELLs are invisible to it; persist an equity high-water
  mark when that ceiling bites. SELLs still pass: reducing exposure in a
  drawdown is recovery, not risk.
- Position count cap: a BUY of a ticker not yet held halts at MAX_POSITIONS
  open positions; adding to a position already held is exempt.
- Concentration check (ROADMAP P1.3): a sized BUY whose exposure, combined
  with holdings whose daily returns correlate above CORRELATION_THRESHOLD,
  would push the group past settings.max_correlated_pct raises a warning with
  before/after exposure. Warning-only: it never changes the decision, and a
  correlation that cannot be computed is reported as unknown, never as
  independent.
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
from app.models import AgentResult, ConcentrationCheck, PortfolioSummary

logger = logging.getLogger("risk")

VOL_TARGET_PCT = 15.0  # annualized volatility the default position size assumes
SIZE_FLOOR_MULT = 0.25
SIZE_CEIL_MULT = 1.5
FORECAST_VETO_Z = 1.0  # |z| beyond this contradicts the decision
FORECAST_HALVE_Z = 0.5  # z past this (against the decision) halves the size
FORECAST_HORIZON_DAYS = 5
CORRELATION_THRESHOLD = 0.7  # daily-return correlation above this reads as one bet
MIN_RETURN_OBS = 20  # overlapping daily returns needed for a usable correlation


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


def daily_returns(closes: list[float]) -> list[float]:
    """One-day returns; pairs with a nonpositive base price are skipped."""
    return [(b - a) / a for a, b in zip(closes, closes[1:]) if a > 0]


def returns_correlation(
    closes_a: list[float], closes_b: list[float]
) -> float | None:
    """Pearson correlation of daily returns over the overlapping tail.

    Both series end at the latest trading day, so alignment is by length from
    the end. Fewer than MIN_RETURN_OBS overlapping observations (or a flat
    series) returns None: unknown, never zero - missing data must not lower
    the reported risk (ROADMAP P1.3).
    """
    returns_a, returns_b = daily_returns(closes_a), daily_returns(closes_b)
    n = min(len(returns_a), len(returns_b))
    if n < MIN_RETURN_OBS:
        return None
    returns_a, returns_b = returns_a[-n:], returns_b[-n:]
    mean_a = sum(returns_a) / n
    mean_b = sum(returns_b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(returns_a, returns_b))
    var_a = sum((x - mean_a) ** 2 for x in returns_a)
    var_b = sum((y - mean_b) ** 2 for y in returns_b)
    if var_a <= 0 or var_b <= 0:
        return None
    return cov / sqrt(var_a * var_b)


def concentration_check(
    ticker: str,
    size_usd: float,
    portfolio: PortfolioSummary,
    held_closes: dict[str, list[float]] | None = None,
    candidate_closes: list[float] | None = None,
) -> ConcentrationCheck:
    """Correlated-exposure warning for a sized BUY (ROADMAP P1.3).

    Sums the equity share of the candidate (any existing stake included) plus
    every holding whose daily returns correlate with it at or above
    CORRELATION_THRESHOLD, and flags "high" when adding the suggested size
    would push the group past settings.max_correlated_pct, with before/after
    exposure in the result. Warning-only by design: the caps above already
    block trades; this one exists so correlated positions cannot pass the
    per-ticker limits while behaving like one concentrated bet.
    """
    equity = portfolio.total_equity or 0.0
    if equity <= 0:
        return ConcentrationCheck(
            status="unknown", detail="portfolio value unavailable"
        )

    def held_value(position) -> float:
        return position.value if position.value is not None else position.cost

    correlated: list[str] = []
    unverified: list[str] = []
    group_value = sum(
        held_value(p) for p in portfolio.positions if p.ticker == ticker
    )
    for position in portfolio.positions:
        if position.ticker == ticker:
            continue  # already in the group: correlation with itself is 1
        r = returns_correlation(
            candidate_closes or [], (held_closes or {}).get(position.ticker, [])
        )
        if r is None:
            unverified.append(position.ticker)
        elif r >= CORRELATION_THRESHOLD:
            correlated.append(position.ticker)
            group_value += held_value(position)

    before_pct = group_value / equity
    after_pct = (group_value + size_usd) / equity
    cap = settings.max_correlated_pct
    if after_pct > cap + 1e-9:
        return ConcentrationCheck(
            status="high",
            detail=(
                f"{ticker} plus correlated holdings "
                f"({', '.join(correlated) or 'none'}) would move group exposure "
                f"from {before_pct:.1%} to {after_pct:.1%} of equity, past the "
                f"{cap:.0%} correlated-group cap"
            ),
            correlated=correlated,
            unverified=unverified,
            before_pct=round(before_pct * 100, 1),
            after_pct=round(after_pct * 100, 1),
            cap_pct=round(cap * 100, 1),
        )
    if unverified:
        return ConcentrationCheck(
            status="unknown",
            detail=(
                f"no usable price history to correlate "
                f"{', '.join(unverified)} with {ticker}; their overlap with the "
                f"position is unverified"
            ),
            correlated=correlated,
            unverified=unverified,
            before_pct=round(before_pct * 100, 1),
            after_pct=round(after_pct * 100, 1),
            cap_pct=round(cap * 100, 1),
        )
    return ConcentrationCheck(
        status="ok",
        correlated=correlated,
        before_pct=round(before_pct * 100, 1),
        after_pct=round(after_pct * 100, 1),
        cap_pct=round(cap * 100, 1),
    )


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
        flags.append(
            f"confidence capped at 50%: {failed}/{len(analysts)} analyst inputs failed"
        )

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
            pnl = portfolio.total_pnl
            if pnl is not None and pnl < 0 and equity - pnl > 0:
                # ponytail: total_pnl is open positions' unrealized P&L only,
                # so realized losses drop out of the brake once a SELL settles;
                # persist an equity high-water mark when that ceiling bites
                drawdown = -pnl / (equity - pnl)
                if drawdown > settings.max_drawdown_pct + 1e-9:
                    reasons.append(
                        f"open positions are {drawdown:.0%} underwater, past "
                        f"the {settings.max_drawdown_pct:.0%} drawdown brake"
                    )
            held_tickers = {p.ticker for p in portfolio.positions}
            if ticker not in held_tickers and len(held_tickers) >= settings.max_positions:
                reasons.append(
                    f"open positions would reach {len(held_tickers) + 1}, past "
                    f"the {settings.max_positions}-position cap"
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
