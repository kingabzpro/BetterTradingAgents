"""SEC EDGAR fundamentals, served as filed (ROADMAP P1.15).

The backtest harness used to exclude the fundamental analyst because Finnhub
and yfinance serve only current-vintage statements: replaying them on a past
date leaks later information. SEC EDGAR records the date every figure was
filed, needs no key, and covers US filers, so a replay dated T can read the
statements exactly as they stood that day - a period that has ended but has
not been filed yet is not served, and a figure restated later still reads as
first reported.

The interesting logic is pure and offline-testable: `select_as_filed` reduces
raw companyfacts rows to the as-of view, and `fundamentals_as_of` turns that
view into the normalized metric keys the fundamental analyst already reads.
Only `fetch_companyfacts` / `fetch_fundamentals` touch the network.

SEC asks callers to identify themselves and refuses requests without a
contact address, so a default User-Agent is sent. Set `SEC_EDGAR_USER_AGENT`
to your own so SEC can reach you rather than the project.
"""

import logging
from datetime import date, timedelta
from math import isfinite


from app.config import settings
from app.tools.http_client import get_client

logger = logging.getLogger("sec_edgar")

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
DEFAULT_USER_AGENT = (
    "BetterTradingAgents/0.1 (educational research; contact@example.com)"
)

# Concept candidates per metric: filers tag the same line differently and
# sometimes abandon a tag while a sibling stays live, so pick the candidate
# carrying the most recent data rather than the first one with any rows.
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
EQUITY_TAGS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)

QUARTER_DAYS = (75, 105)  # ~13-week period; annual (10-K) rows are ~365
ANNUAL_DAYS = (350, 380)

# In-process caches: one ticker map and one companyfacts payload per ticker per
# process. The per-date snapshot cache (app/backtest/cache.py) handles warm
# re-runs across processes.
_ticker_map: dict[str, int] = {}
_facts_cache: dict[str, dict | None] = {}


def _headers() -> dict:
    return {
        "User-Agent": settings.sec_edgar_user_agent or DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }


def _period_days(item: dict) -> int | None:
    start, end = item.get("start"), item.get("end")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def select_as_filed(units: list[dict], as_of: str) -> list[dict]:
    """Reduce raw fact rows to the as-of view, first reported.

    Rows are grouped by period (start, end) - or by end alone for balance-sheet
    items, which carry no start. For each period the row with the earliest
    `filed` date wins, so a figure restated later still reads as first
    reported; a period whose earliest filing lands after `as_of` is dropped,
    so a fiscal period that has ended but not been filed yet is never served.
    Rows without a value, an end date, or a filing date are discarded.
    """
    best: dict[tuple, dict] = {}
    for item in units or []:
        end = item.get("end")
        value = item.get("val")
        filed = str(item.get("filed") or "")
        if end is None or value is None or not filed:
            continue
        key = (item.get("start"), end)
        if key not in best or filed < best[key]["filed"]:
            best[key] = {
                "start": item.get("start"),
                "end": end,
                "val": value,
                "filed": filed,
            }
    return [row for row in best.values() if row["filed"] <= as_of]


def _quarterly(rows: list[dict]) -> list[dict]:
    lo, hi = QUARTER_DAYS
    out = [r for r in rows if (d := _period_days(r)) is not None and lo <= d <= hi]
    out.sort(key=lambda r: r["end"])
    return out


def _annual(rows: list[dict]) -> list[dict]:
    lo, hi = ANNUAL_DAYS
    out = [r for r in rows if (d := _period_days(r)) is not None and lo <= d <= hi]
    out.sort(key=lambda r: r["end"])
    return out


def _instant(rows: list[dict]) -> list[dict]:
    out = [r for r in rows if not r.get("start")]
    out.sort(key=lambda r: r["end"])
    return out


def _ttm(quarters: list[dict], as_of: str, offset_days: int = 0) -> float | None:
    """Sum of the four most recent quarterly values ending by the cutoff."""
    cutoff = (date.fromisoformat(as_of) - timedelta(days=offset_days)).isoformat()
    usable = [r for r in quarters if r["end"] <= cutoff]
    if len(usable) < 4:
        return None
    return sum(r["val"] for r in usable[-4:])


def _flow_value(rows: list[dict], as_of: str) -> float | None:
    """Trailing-twelve-month value, falling back to the latest annual figure."""
    ttm = _ttm(_quarterly(rows), as_of)
    if ttm is not None:
        return ttm
    annual = [r for r in _annual(rows) if r["end"] <= as_of]
    return annual[-1]["val"] if annual else None


def _growth_pct(rows: list[dict], as_of: str) -> float | None:
    """Year-over-year change of the trailing-twelve-month value, in percent."""
    quarters = _quarterly(rows)
    current = _ttm(quarters, as_of)
    prior = _ttm(quarters, as_of, offset_days=365)
    if current is None or prior is None:
        annual = [r for r in _annual(rows) if r["end"] <= as_of]
        if len(annual) >= 2:
            current, prior = annual[-1]["val"], annual[-2]["val"]
    if current is None or not prior:
        return None
    return round((current / prior - 1) * 100, 2)


def _instant_value(rows: list[dict], as_of: str) -> float | None:
    points = [r for r in _instant(rows) if r["end"] <= as_of]
    return points[-1]["val"] if points else None


def _unit_rows(facts: dict, tags: tuple[str, ...], unit: str, as_of: str) -> list[dict]:
    """Rows of the candidate tag carrying the most recent data.

    Filers abandon tags: NVDA stopped tagging
    RevenueFromContractWithCustomerExcludingAssessedTax after FY2022 while
    Revenues stayed live, so "first tag with rows" serves years-stale data
    (and looks correct on the many filers that never switched). The tag whose
    latest period ends last wins; ties keep the earlier candidate's order.
    """
    best: list[dict] = []
    best_end = ""
    for tag in tags:
        node = facts.get(tag)
        if not isinstance(node, dict):
            continue
        rows = select_as_filed(node.get("units", {}).get(unit, []), as_of)
        latest = max((r["end"] for r in rows), default="")
        if latest > best_end:  # ISO dates sort lexicographically
            best, best_end = rows, latest
    return best


def fundamentals_as_of(companyfacts: dict, as_of: str) -> dict:
    """Normalized fundamentals known at `as_of`, from a companyfacts payload.

    Returns the same metric keys the fundamental analyst already reads
    (growth, margins, ROE, EPS), computed from as-filed statements only.
    Valuation multiples (PE, P/B) need a live price and are left out - the
    analyst treats missing keys as missing, not as neutral.
    """
    facts = ((companyfacts or {}).get("facts") or {}).get("us-gaap") or {}
    if not facts:
        return {}

    revenue = _unit_rows(facts, REVENUE_TAGS, "USD", as_of)
    gross = _unit_rows(facts, ("GrossProfit",), "USD", as_of)
    operating = _unit_rows(facts, ("OperatingIncomeLoss",), "USD", as_of)
    net_income = _unit_rows(facts, ("NetIncomeLoss",), "USD", as_of)
    equity = _unit_rows(facts, EQUITY_TAGS, "USD", as_of)
    eps = _unit_rows(facts, ("EarningsPerShareDiluted",), "USD/shares", as_of)

    out: dict = {}
    revenue_ttm = _flow_value(revenue, as_of)
    net_income_ttm = _flow_value(net_income, as_of)
    if revenue_ttm:
        out["revenue_ttm_usd"] = round(revenue_ttm, 2)
        gross_ttm = _flow_value(gross, as_of)
        operating_ttm = _flow_value(operating, as_of)
        if gross_ttm is not None:
            out["gross_margin_pct"] = round(gross_ttm / revenue_ttm * 100, 2)
        if operating_ttm is not None:
            out["operating_margin_pct"] = round(operating_ttm / revenue_ttm * 100, 2)
        if net_income_ttm is not None:
            out["net_margin_pct"] = round(net_income_ttm / revenue_ttm * 100, 2)
        growth = _growth_pct(revenue, as_of)
        if growth is not None:
            out["revenue_growth_ttm_pct"] = growth
    if net_income_ttm is not None:
        out["net_income_ttm_usd"] = round(net_income_ttm, 2)
        earnings_growth = _growth_pct(net_income, as_of)
        if earnings_growth is not None:
            out["earnings_growth_ttm_pct"] = earnings_growth
        equity_now = _instant_value(equity, as_of)
        if equity_now:
            out["roe_pct"] = round(net_income_ttm / equity_now * 100, 2)
    eps_ttm = _ttm(_quarterly(eps), as_of)
    if eps_ttm is None:
        annual_eps = [r for r in _annual(eps) if r["end"] <= as_of]
        eps_ttm = annual_eps[-1]["val"] if annual_eps else None
    if eps_ttm is not None and isfinite(eps_ttm):
        out["eps_ttm"] = round(eps_ttm, 2)
    return out


async def _get_json(url: str) -> dict:
    client = get_client()
    response = await client.get(url, headers=_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


async def _cik_for(ticker: str) -> int | None:
    ticker = ticker.upper()
    if ticker not in _ticker_map:
        payload = await _get_json(SEC_TICKERS_URL)
        for row in payload.values():
            symbol = str(row.get("ticker", "")).upper()
            cik = row.get("cik_str")
            if symbol and cik is not None:
                _ticker_map[symbol] = int(cik)
    return _ticker_map.get(ticker)


async def fetch_companyfacts(ticker: str) -> dict | None:
    """Raw companyfacts payload for a US filer, or None when EDGAR has none."""
    ticker = ticker.upper()
    if ticker in _facts_cache:
        return _facts_cache[ticker]
    cik = await _cik_for(ticker)
    facts = await _get_json(SEC_FACTS_URL.format(cik=cik)) if cik else None
    _facts_cache[ticker] = facts
    return facts


async def fetch_fundamentals(ticker: str, as_of: str) -> dict:
    """As-filed fundamentals payload for one ticker at `as_of`.

    Mirrors app.backtest.data.fetch_fundamentals so the snapshot builder can
    swap vendors: {"company_name", "fundamentals", "source"}.
    """
    facts = await fetch_companyfacts(ticker)
    if not facts:
        return {"company_name": ticker, "fundamentals": {}, "source": "none"}
    return {
        "company_name": facts.get("entityName") or ticker,
        "fundamentals": fundamentals_as_of(facts, as_of),
        "source": "sec_edgar",
    }
