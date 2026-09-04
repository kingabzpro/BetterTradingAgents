"""Lightweight stock discovery for the "I Am Feeling Lucky" action.

Ranking follows the published cross-sectional momentum literature:

- 3-6 month formation returns predict continuation (Jegadeesh & Titman 1993,
  Journal of Finance), so the score weighs 63- and 126-day returns.
- The most recent month reverses instead of continuing (Jegadeesh 1990,
  Journal of Finance; Lehmann 1990, QJE) - the reason academics use 12-1
  momentum - so formation windows skip the last 21 sessions and a positive
  last-month return is penalized rather than rewarded.
- Smooth, continuous price paths carry more persistent momentum than jumpy
  spikes ("frog in the pan", Da, Gurun & Warachka 2014, RFS), scored as the
  share of up days minus down days over the formation window.
- Nearness to the 52-week high predicts returns better than raw momentum and
  does not revert long-term (George & Hwang 2004, Journal of Finance).
- The momentum term is volatility-scaled, which halves momentum-crash risk
  (Barroso & Santa-Clara 2015, JFE; Moreira & Muir 2017, JPE).
"""

import asyncio
import logging
from math import sqrt
from statistics import mean, pstdev
from threading import Lock
from time import monotonic

import yfinance as yf

from app.outlook import Outlook

logger = logging.getLogger("discovery")
MIN_MARKET_CAP_USD = 1_000_000_000
MAX_MARKET_CAP_USD = 50_000_000_000
MIN_REVENUE_GROWTH_PCT = 10
MIN_TRAILING_REVENUE_USD = 100_000_000
MIN_AVG_DAILY_VOLUME = 500_000
SCREEN_SIZE = 60
CACHE_TTL_SECONDS = 60 * 60
MIN_BARS = 150  # enough history for the 6-month skip-month formation window
REVERSAL_WEIGHT = 0.25  # penalty on the 21-day return (short-term reversal)
SMOOTHNESS_POINTS = 3.0  # frog-in-the-pan weight (Da et al. 2014)
HIGH_POINTS = 2.0  # 52-week-high proximity weight (George & Hwang 2004)

_candidate_cache: tuple[
    float, tuple[dict[str, list[float]], dict[str, float]]
] | None = None
_candidate_cache_lock = Lock()

# (63-day, 126-day) continuation weights by horizon; the day-trade horizon
# still leans on the 3-month window because 1-month momentum is a reversal,
# not a continuation, signal (Jegadeesh 1990).
CONTINUATION_WEIGHTS = {
    "day_trade": (0.50, 0.25),
    "short_term": (0.40, 0.35),
    "long_term": (0.20, 0.55),
}


async def discover_stocks(outlook: Outlook, limit: int = 5) -> dict:
    """Return the strongest risk-adjusted momentum candidates right now."""
    (histories, market_caps), cache_hit = await asyncio.to_thread(_cached_candidates)
    ranked = rank_candidates(histories, market_caps, outlook, limit)
    if len(ranked) < limit:
        raise ValueError("not enough current market data to select five candidates")
    return {
        "tickers": [item["ticker"] for item in ranked],
        "universe_size": len(histories),
        "minimum_market_cap_usd": MIN_MARKET_CAP_USD,
        "maximum_market_cap_usd": MAX_MARKET_CAP_USD,
        "minimum_revenue_growth_pct": MIN_REVENUE_GROWTH_PCT,
        "cached": cache_hit,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "method": (
            "growth eligibility plus momentum research: 3-6 month continuation "
            "minus last-month reversal (Jegadeesh & Titman 1993; Jegadeesh 1990), "
            "path smoothness (Da et al. 2014), 52-week-high proximity "
            "(George & Hwang 2004), volatility-scaled (Barroso & Santa-Clara 2015)"
        ),
        "disclaimer": "Candidates are not guaranteed to be profitable or investment advice.",
    }


def rank_candidates(
    histories: dict[str, list[float]],
    market_caps: dict[str, float],
    outlook: Outlook,
    limit: int = 5,
) -> list[dict]:
    """Rank histories with a small, explainable, research-grounded score."""
    w63, w126 = CONTINUATION_WEIGHTS[outlook]
    ranked = []
    for ticker, closes in histories.items():
        market_cap = market_caps.get(ticker, 0)
        if (
            market_cap <= MIN_MARKET_CAP_USD
            or market_cap >= MAX_MARKET_CAP_USD
            or len(closes) < MIN_BARS
            or any(price <= 0 for price in closes[-MIN_BARS:])
        ):
            continue
        ret21 = _pct_change(closes, 21)
        # Formation returns skip the most recent month, the standard 12-1
        # construction (Jegadeesh 1990; Jegadeesh & Titman 1993): the last
        # month reverses, so a fresh +35% blow-off drops out of the signal.
        ret63 = _skip_month_change(closes, 63)
        ret126 = _skip_month_change(closes, 126)
        daily = [cur / prev - 1 for prev, cur in zip(closes[-64:-1], closes[-63:])]
        volatility = pstdev(daily) * sqrt(252) * 100
        # Penalize only a positive last-month return (late-stage blow-off).
        # Rewarding a negative one would turn this into a bottom-fishing
        # reversal screen, which feeds the manager crashed bouncers instead
        # of intact trends.
        momentum = w63 * ret63 + w126 * ret126 - REVERSAL_WEIGHT * max(ret21, 0.0)
        # Frog in the pan (Da et al. 2014): continuous upward drift persists,
        # discrete jumps fade. Up-day dominance is not re-signed for losers
        # because this is a long-only candidate list.
        up_days = sum(1 for prev, cur in zip(closes[-64:-1], closes[-63:]) if cur > prev)
        smoothness = 2 * up_days / 63 - 1
        # 52-week-high proximity (George & Hwang 2004), from the 1-year window.
        high_proximity = closes[-1] / max(closes)
        sma20, sma50 = mean(closes[-20:]), mean(closes[-50:])
        trend = (1 if closes[-1] > sma20 else -1) + (1.5 if sma20 > sma50 else -1.5)
        score = (
            momentum / max(volatility / 20, 0.75)
            + SMOOTHNESS_POINTS * smoothness
            + HIGH_POINTS * high_proximity
            + trend
        )
        ranked.append(
            {"ticker": ticker, "score": round(score, 4), "market_cap_usd": market_cap}
        )
    return sorted(ranked, key=lambda item: (-item["score"], item["ticker"]))[:limit]


def _pct_change(closes: list[float], days: int) -> float:
    return (closes[-1] / closes[-days - 1] - 1) * 100


def _skip_month_change(closes: list[float], days: int) -> float:
    """Percent change over a `days` window that skips the last 21 sessions."""
    return (closes[-22] / closes[-days - 22] - 1) * 100


def _cached_candidates() -> tuple[
    tuple[dict[str, list[float]], dict[str, float]], bool
]:
    """Reuse one provider snapshot for an hour and prevent duplicate refreshes."""
    global _candidate_cache
    with _candidate_cache_lock:
        now = monotonic()
        if _candidate_cache and now - _candidate_cache[0] < CACHE_TTL_SECONDS:
            return _candidate_cache[1], True
        candidates = _download_candidates()
        if len(candidates[0]) >= 5:
            _candidate_cache = (monotonic(), candidates)
        return candidates, False


def _download_candidates() -> tuple[dict[str, list[float]], dict[str, float]]:
    market_caps = _screen_growth_candidates()
    eligible = list(market_caps)
    if not eligible:
        return {}, {}
    try:
        data = yf.download(
            " ".join(eligible),
            period="1y",
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
