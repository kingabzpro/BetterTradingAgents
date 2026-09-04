"""Lightweight stock discovery for the "I Am Feeling Lucky" action."""

import asyncio
import logging
from math import sqrt
from statistics import mean, pstdev

import yfinance as yf

from app.outlook import Outlook

logger = logging.getLogger("discovery")
MIN_MARKET_CAP_USD = 1_000_000_000
MAX_MARKET_CAP_USD = 50_000_000_000
MIN_REVENUE_GROWTH_PCT = 10
MIN_TRAILING_REVENUE_USD = 100_000_000
MIN_AVG_DAILY_VOLUME = 500_000
SCREEN_SIZE = 60

MOMENTUM_WEIGHTS = {
    "day_trade": (0.60, 0.30, 0.10),
    "short_term": (0.20, 0.40, 0.40),
    "long_term": (0.00, 0.20, 0.80),
}


async def discover_stocks(outlook: Outlook, limit: int = 5) -> dict:
    """Return the strongest risk-adjusted momentum candidates right now."""
    histories, market_caps = await asyncio.to_thread(_download_candidates)
    ranked = rank_candidates(histories, market_caps, outlook, limit)
    if len(ranked) < limit:
        raise ValueError("not enough current market data to select five candidates")
    return {
        "tickers": [item["ticker"] for item in ranked],
        "universe_size": len(histories),
        "minimum_market_cap_usd": MIN_MARKET_CAP_USD,
        "maximum_market_cap_usd": MAX_MARKET_CAP_USD,
        "minimum_revenue_growth_pct": MIN_REVENUE_GROWTH_PCT,
        "method": "growth eligibility plus horizon-weighted, volatility-adjusted momentum",
        "disclaimer": "Candidates are not guaranteed to be profitable or investment advice.",
    }


def rank_candidates(
    histories: dict[str, list[float]],
    market_caps: dict[str, float],
    outlook: Outlook,
    limit: int = 5,
) -> list[dict]:
    """Rank histories with a small, explainable momentum/trend score."""
    weights = MOMENTUM_WEIGHTS[outlook]
    ranked = []
    for ticker, closes in histories.items():
        market_cap = market_caps.get(ticker, 0)
        if (
            market_cap <= MIN_MARKET_CAP_USD
            or market_cap >= MAX_MARKET_CAP_USD
            or len(closes) < 64
            or any(price <= 0 for price in closes[-64:])
        ):
            continue
        returns = [_pct_change(closes, days) for days in (5, 21, 63)]
        daily = [cur / prev - 1 for prev, cur in zip(closes[-64:-1], closes[-63:])]
        volatility = pstdev(daily) * sqrt(252) * 100
        momentum = sum(weight * change for weight, change in zip(weights, returns))
        sma20, sma50 = mean(closes[-20:]), mean(closes[-50:])
        trend = (1 if closes[-1] > sma20 else -1) + (1.5 if sma20 > sma50 else -1.5)
        score = momentum / max(volatility / 20, 0.75) + trend
        ranked.append(
            {"ticker": ticker, "score": round(score, 4), "market_cap_usd": market_cap}
        )
    return sorted(ranked, key=lambda item: (-item["score"], item["ticker"]))[:limit]


def _pct_change(closes: list[float], days: int) -> float:
    return (closes[-1] / closes[-days - 1] - 1) * 100


def _download_candidates() -> tuple[dict[str, list[float]], dict[str, float]]:
    market_caps = _screen_growth_candidates()
    eligible = list(market_caps)
    if not eligible:
        return {}, {}
    try:
        data = yf.download(
            " ".join(eligible),
            period="6mo",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
            group_by="ticker",
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 - provider failures become a useful API error
        logger.warning("stock discovery download failed: %s", exc)
        return {}, market_caps

    histories = {}
    for ticker in eligible:
        try:
            closes = data[ticker]["Close"].dropna().tolist()
        except (KeyError, TypeError):
            continue
        values = [float(value) for value in closes]
        if values:
            histories[ticker] = values
    return histories, market_caps


def _screen_growth_candidates() -> dict[str, float]:
    """Find liquid, growing $1B-$50B U.S. companies with room to break out."""
    try:
        query = yf.EquityQuery(
            "and",
            [
                yf.EquityQuery("eq", ["region", "us"]),
                yf.EquityQuery("is-in", ["exchange", "NMS", "NYQ", "ASE"]),
                yf.EquityQuery("gt", ["intradaymarketcap", MIN_MARKET_CAP_USD]),
                yf.EquityQuery("lt", ["intradaymarketcap", MAX_MARKET_CAP_USD]),
                yf.EquityQuery(
                    "gt", ["totalrevenues.lasttwelvemonths", MIN_TRAILING_REVENUE_USD]
                ),
                yf.EquityQuery(
                    "gt",
                    ["totalrevenues1yrgrowth.lasttwelvemonths", MIN_REVENUE_GROWTH_PCT],
                ),
                yf.EquityQuery("gt", ["avgdailyvol3m", MIN_AVG_DAILY_VOLUME]),
            ],
        )
        quotes = yf.screen(
            query,
            size=SCREEN_SIZE,
            sortField="totalrevenues1yrgrowth.lasttwelvemonths",
            sortAsc=False,
        ).get("quotes", [])
    except Exception as exc:  # noqa: BLE001 - provider failures become a useful API error
        logger.warning("market-cap screen failed: %s", exc)
        return {}
    return {
        quote["symbol"]: float(quote.get("marketCap") or quote.get("intradaymarketcap"))
        for quote in quotes
        if quote.get("symbol")
        and isinstance(
            quote.get("marketCap") or quote.get("intradaymarketcap"), (int, float)
        )
        and (quote.get("marketCap") or quote.get("intradaymarketcap")) > MIN_MARKET_CAP_USD
        and (quote.get("marketCap") or quote.get("intradaymarketcap")) < MAX_MARKET_CAP_USD
    }
