"""Technical indicators computed in plain Python.

The LLM never calculates numbers; these functions do it and the Technical
Analyst agent only interprets the results.
"""

from statistics import mean, pstdev


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return mean(values[-window:])


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Simple-average RSI (not Wilder smoothing - good enough for an MVP)."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[-(period + 1) :], closes[-period:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def pct_change(closes: list[float], lookback_days: int) -> float | None:
    """Percent change over the last N trading days."""
    if len(closes) < lookback_days + 1 or closes[-lookback_days - 1] == 0:
        return None
    return (closes[-1] / closes[-lookback_days - 1] - 1.0) * 100.0


def annualized_volatility(closes: list[float], window: int = 20) -> float | None:
    """Annualized stdev of daily returns over the last `window` days."""
    if len(closes) < window + 1:
        return None
    returns = [
        cur / prev - 1.0 for prev, cur in zip(closes[-(window + 1) :], closes[-window:])
    ]
    return pstdev(returns) * (252**0.5) * 100.0


def compute_indicators(closes: list[float]) -> dict:
    """All indicators the Technical Analyst gets to interpret."""
    if len(closes) < 2:
        return {"error": "not enough price history"}

    price = closes[-1]
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    momentum = pct_change(closes, 10)

    return {
        "price": round(price, 2),
        "sma_20": round(sma20, 2) if sma20 else None,
        "sma_50": round(sma50, 2) if sma50 else None,
        "rsi_14": round(rsi(closes), 1) if rsi(closes) is not None else None,
        "momentum_10d_pct": round(momentum, 2) if momentum is not None else None,
        "volatility_annualized_pct": (
            round(annualized_volatility(closes), 2)
            if annualized_volatility(closes) is not None
            else None
        ),
        "change_1d_pct": _safe_round(pct_change(closes, 1)),
        "change_5d_pct": _safe_round(pct_change(closes, 5)),
        "change_21d_pct": _safe_round(pct_change(closes, 21)),
        "change_63d_pct": _safe_round(pct_change(closes, 63)),
        "price_above_sma20": (price > sma20) if sma20 else None,
        "sma20_above_sma50": (sma20 > sma50) if (sma20 and sma50) else None,
        "history_days": len(closes),
    }


def _safe_round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None
