"""Market data for one ticker, merged from three providers:

- Finnhub  -> company profile, fundamental metrics and company news ("info")
- Olostep  -> web news search + article scraping (fallback when Finnhub is thin),
              and the Reddit / StockTwits social search behind the Sentiment
              Analyst (ROADMAP 3.1)
- yfinance -> historical daily closes (all technical indicators are computed
              from these) and as a general fallback for fundamentals/news

Everything degrades gracefully: a missing key or a failing provider just means
we fall back to the next source.
"""

import asyncio
import json
import logging
from math import isfinite
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from urllib.parse import urlparse

import httpx
import yfinance as yf

from app.config import settings

logger = logging.getLogger("market")

FINNHUB_BASE = "https://finnhub.io/api/v1"
OLOSTEP_SEARCH = "https://api.olostep.com/v1/searches"
OLOSTEP_SCRAPE = "https://api.olostep.com/v1/scrapes"
TIMEGPT_FORECAST = "https://api.nixtla.io/v2/forecast"

_price_cache: dict[str, tuple[float, float]] = {}  # ticker -> (price, fetched_at)
_CACHE_TTL = 60.0

# Finnhub /stock/metric keys -> normalized fundamentals keys (all percents as numbers).
FINNHUB_METRICS = {
    "peTTM": "pe_ratio_ttm",
    "forwardPE": "pe_ratio_forward",
    "pbQuarterly": "price_to_book",
    "revenueGrowthTTMYoy": "revenue_growth_ttm_pct",
    "epsGrowthTTMYoy": "earnings_growth_ttm_pct",
    "grossMarginTTM": "gross_margin_pct",
    "netProfitMarginTTM": "net_margin_pct",
    "operatingMarginTTM": "operating_margin_pct",
    "roeTTM": "roe_pct",
    "beta": "beta",
    "dividendYieldIndicatedAnnual": "dividend_yield_pct",
    "52WeekHigh": "week_52_high",
    "52WeekLow": "week_52_low",
}
FINNHUB_EPS_KEYS = ("epsTTM", "epsExclExtraTTM", "epsAnnual")


@dataclass
class MarketData:
    ticker: str
    price: float | None = None
    company_name: str = ""
    closes: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    fundamentals: dict = field(default_factory=dict)
    news: list[dict] = field(default_factory=list)
    social: list[dict] = field(default_factory=list)  # Reddit / StockTwits posts
    sources: dict = field(default_factory=dict)  # prices / fundamentals / news / social
    as_of: str = ""


async def get_stock_data(ticker: str) -> MarketData:
    """Fetch everything the agents need for one ticker, concurrently."""
    yf_task = asyncio.create_task(_yf_all(ticker))
    fundamentals_task = asyncio.create_task(_finnhub_fundamentals(ticker))
    news_task = asyncio.create_task(_finnhub_news(ticker))
    social_task = asyncio.create_task(_olostep_social(ticker))

    yf_data, finnhub_fund, finnhub_news, social = await asyncio.gather(
        yf_task, fundamentals_task, news_task, social_task
    )

    data = MarketData(ticker=ticker)
    data.closes = yf_data["closes"]
    data.highs = yf_data["highs"]
    data.lows = yf_data["lows"]
    data.volumes = yf_data["volumes"]
    data.price = yf_data["price"]
    if data.price is None:
        raise ValueError(f"no price data found for '{ticker}'")

    data.company_name = (
        finnhub_fund.pop("company_name", "") or yf_data["name"] or ticker
    )

    # Fundamentals: Finnhub wins on conflicts, yfinance fills the gaps.
    merged = {**yf_data["fundamentals"], **finnhub_fund}
    data.fundamentals = {k: v for k, v in merged.items() if v is not None}
    data.sources["fundamentals"] = (
        "finnhub" if finnhub_fund else ("yfinance" if data.fundamentals else "none")
    )

    # News: Finnhub -> Olostep search/scrape -> yfinance.
    if finnhub_news:
        data.news, data.sources["news"] = finnhub_news, "finnhub"
    else:
        olostep_news = await _olostep_news(ticker, data.company_name)
        if olostep_news:
            data.news, data.sources["news"] = olostep_news, "olostep"
        else:
            data.news, data.sources["news"] = yf_data["news"], "yfinance"
    # Social sentiment posts (ROADMAP 3.1): Olostep site-restricted search.
    data.social, data.sources["social"] = social, ("olostep" if social else "none")
    data.sources["prices"] = "yfinance"
    data.as_of = datetime.now(timezone.utc).isoformat()

    logger.info(
        "[%s] data ready: fundamentals=%s news=%s (%d items), social=%s (%d posts)",
        ticker,
        data.sources["fundamentals"],
        data.sources["news"],
        len(data.news),
        data.sources["social"],
        len(data.social),
    )
    return data


async def get_current_price(ticker: str) -> float | None:
    """Lightweight price lookup with a short cache (used by the portfolio)."""
    cached = _price_cache.get(ticker)
    if cached and monotonic() - cached[1] < _CACHE_TTL:
        return cached[0]
    price = await asyncio.to_thread(_yf_price, ticker)
    if price is not None:
        _price_cache[ticker] = (price, monotonic())
    return price


async def get_closes_between(ticker: str, start: str, end: str) -> dict[str, float]:
    """Daily closes as {ISO date: close} between start and end (inclusive).

    Used by decision memory to grade past calls against realized prices.
    """
    return await asyncio.to_thread(_yf_closes_between, ticker, start, end)


def _yf_closes_between(ticker: str, start: str, end: str) -> dict[str, float]:
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end, interval="1d")
    except Exception as exc:  # noqa: BLE001 - yfinance raises many shapes
        logger.warning("[%s] close history fetch failed: %s", ticker, exc)
        return {}
    if hist.empty:
        return {}
    return {
        ts.strftime("%Y-%m-%d"): round(float(close), 4)
        for ts, close in hist["Close"].dropna().items()
    }


async def get_ohlcv_between(ticker: str, start: str, end: str) -> dict:
    """OHLCV history between dates, for point-in-time (backtest) snapshots.

    `price` is the last close in the window - the price known at `end`.
    """
    return await asyncio.to_thread(_yf_ohlcv_between, ticker, start, end)


def _yf_ohlcv_between(ticker: str, start: str, end: str) -> dict:
    out: dict = {"closes": [], "highs": [], "lows": [], "volumes": [], "price": None}
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end, interval="1d")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] OHLCV history fetch failed: %s", ticker, exc)
        return out
    if hist.empty:
        return out
    hist = hist.dropna(subset=["Close"])
    out["closes"] = [round(float(c), 4) for c in hist["Close"].tolist()]
    out["highs"] = [round(float(h), 4) for h in hist["High"].tolist()]
    out["lows"] = [round(float(l), 4) for l in hist["Low"].tolist()]
    out["volumes"] = [int(v) for v in hist["Volume"].tolist()]
    out["price"] = out["closes"][-1] if out["closes"] else None
    return out


async def get_news_between(ticker: str, from_date: str, to_date: str) -> list[dict]:
    """Company news published in [from_date, to_date] - historical windows for
    backtests (the caller still filters to `published <= to_date`)."""
    return await _finnhub_news(
        ticker,
        datetime.strptime(from_date, "%Y-%m-%d").date(),
        datetime.strptime(to_date, "%Y-%m-%d").date(),
    )


async def get_timegpt_forecast(
    closes: list[float], horizon: int = 5
) -> dict | None:
    """Ask TimeGPT for a zero-shot forecast; return None for the local fallback."""
    if not settings.nixtla_api_key or horizon < 1 or len(closes) < 30:
        return None
    sample = closes[-512:]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                TIMEGPT_FORECAST,
                headers={"Authorization": f"Bearer {settings.nixtla_api_key}"},
                json={
                    "freq": "D",
                    "h": horizon,
                    "series": {"sizes": [len(sample)], "y": sample},
                    "model": "timegpt-1",
                    "finetune_steps": 0,
                },
            )
            response.raise_for_status()
            return _normalize_timegpt_forecast(response.json(), closes[-1], horizon)
    except Exception as exc:  # noqa: BLE001 - provider failure must degrade gracefully
        logger.warning("TimeGPT forecast failed: %s", exc)
        return None


def _normalize_timegpt_forecast(
    payload: dict, current_price: float, horizon: int
) -> dict | None:
    means = payload.get("mean")
    if (
        not isinstance(means, list)
        or len(means) < horizon
        or not isinstance(means[horizon - 1], (int, float))
    ):
        return None
    price = float(means[horizon - 1])
    if current_price <= 0 or price <= 0 or not isfinite(price):
        return None
    return {
        "forecast_price_5d": round(price, 2),
        "forecast_change_5d_pct": round((price / current_price - 1) * 100, 2),
        "forecast_trend_r2": None,
        "forecast_window_days": None,
        "forecast_horizon_days": horizon,
        "forecast_method": "timegpt-1",
    }


# --------------------------------------------------------------------------
# yfinance: price history (always required) + fallback fundamentals/news
# --------------------------------------------------------------------------


async def _yf_all(ticker: str) -> dict:
    """History, profile and news fetched concurrently (3 round-trips -> 1 wait)."""
    history, profile, news = await asyncio.gather(
        asyncio.to_thread(_yf_history, ticker),
        asyncio.to_thread(_yf_profile, ticker),
        asyncio.to_thread(_yf_news, ticker),
    )
    return {**history, **profile, **news}


def _yf_history(ticker: str) -> dict:
    out = {"closes": [], "highs": [], "lows": [], "volumes": [], "price": None}
    hist = yf.Ticker(ticker).history(period="6mo", interval="1d")
    if not hist.empty:
        hist = hist.dropna(subset=["Close"])
        out["closes"] = [round(float(c), 4) for c in hist["Close"].tolist()]
        out["highs"] = [round(float(h), 4) for h in hist["High"].tolist()]
        out["lows"] = [round(float(l), 4) for l in hist["Low"].tolist()]
        out["volumes"] = [int(v) for v in hist["Volume"].tolist()]
        if out["closes"]:
            out["price"] = out["closes"][-1]
    if out["price"] is None:
        out["price"] = _yf_price(ticker)
    return out


def _yf_profile(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info or {}
        return {
            "name": info.get("shortName") or info.get("longName") or ticker,
            "fundamentals": _map_yf_fundamentals(info),
        }
    except Exception as exc:  # noqa: BLE001 - yfinance raises many shapes
        logger.warning("[%s] yfinance info failed: %s", ticker, exc)
        return {"name": ticker, "fundamentals": {}}


def _yf_news(ticker: str) -> dict:
    try:
        return {"news": _normalize_yf_news(yf.Ticker(ticker).news or [])[:6]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] yfinance news failed: %s", ticker, exc)
        return {"news": []}


def _yf_price(ticker: str) -> float | None:
    try:
        price = yf.Ticker(ticker).fast_info.get("lastPrice")
        return float(price) if price else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] price fetch failed: %s", ticker, exc)
        return None


def _map_yf_fundamentals(info: dict) -> dict:
    """Normalize yfinance fractions (0.31) to percents (31) and unify keys."""
    out: dict = {}
    pairs = [
        ("marketCap", "market_cap", 1),
        ("trailingPE", "pe_ratio_ttm", 1),
        ("forwardPE", "pe_ratio_forward", 1),
        ("priceToBook", "price_to_book", 1),
        ("trailingEps", "eps_ttm", 1),
        ("revenueGrowth", "revenue_growth_ttm_pct", 100),
        ("earningsGrowth", "earnings_growth_ttm_pct", 100),
        ("grossMargins", "gross_margin_pct", 100),
        ("profitMargins", "net_margin_pct", 100),
        ("returnOnEquity", "roe_pct", 100),
        ("beta", "beta", 1),
        ("dividendYield", "dividend_yield_pct", 100),
        ("fiftyTwoWeekHigh", "week_52_high", 1),
        ("fiftyTwoWeekLow", "week_52_low", 1),
    ]
    for key, name, scale in pairs:
        value = info.get(key)
        if isinstance(value, (int, float)):
            out[name] = round(float(value) * scale, 2)
    return out


def _normalize_yf_news(items: list) -> list[dict]:
    """yfinance changed its news payload shape; support both."""
    out = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("content"), dict):
            content = item["content"]
            canonical = (
                content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
            )
            article_url = canonical.get("url", "") if isinstance(canonical, dict) else canonical
            out.append(
                {
                    "title": content.get("title", ""),
                    "source": (content.get("provider") or {}).get("displayName", ""),
                    "published": content.get("pubDate", ""),
                    "summary": "",
                    "url": _safe_http_url(article_url),
                }
            )
        elif isinstance(item, dict) and item.get("title"):
            out.append(
                {
                    "title": item.get("title", ""),
                    "source": item.get("publisher", ""),
                    "published": "",
                    "summary": "",
                    "url": _safe_http_url(item.get("link", "")),
                }
            )
    return [n for n in out if n["title"]]


# --------------------------------------------------------------------------
# Finnhub: profile + metrics + company news
# --------------------------------------------------------------------------


async def _finnhub_get(
    client: httpx.AsyncClient, path: str, ticker: str, extra: dict | None = None
) -> dict:
    params = {"symbol": ticker, "token": settings.finnhub_api_key, **(extra or {})}
    response = await client.get(f"{FINNHUB_BASE}{path}", params=params)
    response.raise_for_status()
    return response.json()


async def _finnhub_fundamentals(ticker: str) -> dict:
    if not settings.finnhub_api_key:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            profile, metrics = await asyncio.gather(
                _finnhub_get(client, "/stock/profile2", ticker),
                _finnhub_get(client, "/stock/metric", ticker, {"metric": "all"}),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] finnhub fundamentals failed: %s", ticker, exc)
        return {}

    out: dict = {}
    name = profile.get("name")
    if name:
        out["company_name"] = name
    cap = profile.get("marketCapitalization")
    if isinstance(cap, (int, float)):  # reported in millions
        out["market_cap"] = round(float(cap) * 1_000_000)

    metric = metrics.get("metric", {})
    for src, dst in FINNHUB_METRICS.items():
        value = metric.get(src)
        if isinstance(value, (int, float)):
            out[dst] = round(float(value), 2)
    for key in FINNHUB_EPS_KEYS:
        if isinstance(metric.get(key), (int, float)):
            out["eps_ttm"] = round(float(metric[key]), 2)
            break
    return out


async def _finnhub_news(
    ticker: str, from_date: date | None = None, to_date: date | None = None
) -> list[dict]:
    if not settings.finnhub_api_key:
        return []
    to_day = to_date or datetime.now(timezone.utc).date()
    from_day = from_date or (to_day - timedelta(days=14))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{FINNHUB_BASE}/company-news",
                params={
                    "symbol": ticker,
                    "from": from_day.isoformat(),
                    "to": to_day.isoformat(),
                    "token": settings.finnhub_api_key,
                },
            )
            response.raise_for_status()
            items = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] finnhub news failed: %s", ticker, exc)
        return []

    items = sorted(items, key=lambda i: i.get("datetime", 0), reverse=True)
    out = []
    for item in items[:6]:
        published = ""
        if item.get("datetime"):
            try:
                published = datetime.fromtimestamp(
                    int(item["datetime"]), tz=timezone.utc
                ).isoformat()
            except (ValueError, OverflowError):
                pass
        out.append(
            {
                "title": item.get("headline", ""),
                "source": item.get("source", ""),
                "published": published,
                "summary": (item.get("summary") or "")[:300],
                "url": _safe_http_url(item.get("url", "")),
            }
        )
    return [n for n in out if n["title"]]


# --------------------------------------------------------------------------
# Olostep: web search (+ scrape of the top article) for news
# --------------------------------------------------------------------------


async def _olostep_news(ticker: str, company_name: str) -> list[dict]:
    if not settings.olostep_api_key:
        return []
    headers = {"Authorization": f"Bearer {settings.olostep_api_key}"}
    query = f"{company_name or ticker} {ticker} stock news latest".strip()
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                OLOSTEP_SEARCH,
                headers=headers,
                json={"query": query, "num_results": 6},
            )
            response.raise_for_status()
            links = _olostep_links(response.json())

            items = []
            for link in links[:6]:
                url = _safe_http_url(link.get("url", ""))
                items.append(
                    {
                        "title": link.get("title", ""),
                        "source": urlparse(url).netloc.replace("www.", ""),
                        "published": "",
                        "summary": (link.get("description") or "")[:300],
                        "url": url,
                    }
                )
            items = [n for n in items if n["title"]]

            # Enrich the top hits with actual article text, scraped in parallel.
            targets = [n for n in items[:2] if n.get("url")]
            markdowns = await asyncio.gather(
                *(_olostep_scrape(client, headers, n["url"]) for n in targets)
            )
            for item, markdown in zip(targets, markdowns):
                if markdown:
                    item["content"] = markdown[:1500]
            return items
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] olostep news failed: %s", ticker, exc)
        return []


def _olostep_links(payload: dict) -> list[dict]:
    result = payload.get("result") or {}
    if isinstance(result.get("links"), list):
        return result["links"]
    raw = result.get("json_content")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed.get("links", [])
        except ValueError:
            return []
    return []


async def _olostep_social(ticker: str) -> list[dict]:
    """Reddit / StockTwits posts about the ticker (ROADMAP 3.1).

    A site-restricted web search, not a dated API: posts are current-vintage,
    so backtest snapshots never call this - they replay with an empty set and
    the sentiment analyst reports a thin-volume neutral.
    """
    if not settings.olostep_api_key:
        return []
    query = f"{ticker} stock (site:reddit.com OR site:stocktwits.com)"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                OLOSTEP_SEARCH,
                headers={"Authorization": f"Bearer {settings.olostep_api_key}"},
                json={"query": query, "num_results": 8},
            )
            response.raise_for_status()
            items = []
            for link in _olostep_links(response.json())[:8]:
                url = _safe_http_url(link.get("url", ""))
                items.append(
                    {
                        "title": link.get("title", ""),
                        "source": urlparse(url).netloc.replace("www.", ""),
                        "published": "",
                        "summary": (link.get("description") or "")[:300],
                        "url": url,
                    }
                )
            return [n for n in items if n["title"]]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] olostep social search failed: %s", ticker, exc)
        return []


def _safe_http_url(value: object) -> str:
    """Return a normal web URL, or an empty string for unsafe schemes."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    return (
        candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""
    )


async def _olostep_scrape(
    client: httpx.AsyncClient, headers: dict, url: str
) -> str | None:
    try:
        response = await client.post(
            OLOSTEP_SCRAPE,
            headers=headers,
            json={"url_to_scrape": url, "formats": ["markdown"]},
        )
        response.raise_for_status()
        return (response.json().get("result") or {}).get("markdown_content")
    except Exception as exc:  # noqa: BLE001
        logger.warning("olostep scrape failed for %s: %s", url, exc)
        return None
