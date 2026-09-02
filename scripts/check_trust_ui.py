"""Offline checks for provenance, backward compatibility, and trust-first UI hooks."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace

import httpx

from app.models import AnalysisRequest, RunStatus, SourceReference, StockAnalysis
from app.tools.market_data import _normalize_yf_news, _safe_http_url
from app.workflow import _source_references


# URL sanitization is enforced both during normalization and response modeling.
assert _safe_http_url("https://example.com/story") == "https://example.com/story"
assert _safe_http_url("javascript:alert(1)") == ""
assert _safe_http_url("file:///secret") == ""
assert SourceReference(
    kind="news", title="Unsafe", provider="test", url="data:text/html,bad"
).url == ""

# Both current and legacy yfinance news payloads retain safe article URLs.
yf_items = _normalize_yf_news(
    [
        {
            "content": {
                "title": "Modern item",
                "provider": {"displayName": "Publisher A"},
                "pubDate": "2026-01-02T10:00:00Z",
                "canonicalUrl": {"url": "https://example.com/modern"},
            }
        },
        {
            "title": "Legacy item",
            "publisher": "Publisher B",
            "link": "https://example.org/legacy",
        },
    ]
)
assert [item["url"] for item in yf_items] == [
    "https://example.com/modern",
    "https://example.org/legacy",
]

# Only display metadata leaves the market-data layer; scraped article content does not.
market = SimpleNamespace(
    sources={"prices": "yfinance", "fundamentals": "finnhub", "news": "olostep"},
    news=[
        {
            "title": "Evidence headline",
            "source": "Example News",
            "published": "2026-01-02T10:00:00Z",
            "url": "https://news.example/evidence",
            "content": "scraped article body must stay internal",
        },
        {"title": "Evidence headline", "url": "https://news.example/evidence"},
    ],
)
references = _source_references(market)
assert len(references) == 1
dumped_reference = references[0].model_dump()
assert dumped_reference["provider"] == "Example News"
assert "content" not in dumped_reference

# New response fields have defaults, so results created by older code still parse.
legacy = StockAnalysis(ticker="MSFT", decision="HOLD")
assert legacy.as_of == "" and legacy.providers == {} and legacy.source_references == []
assert legacy.bull_rebuttal is None and legacy.bear_rebuttal is None
complete = StockAnalysis(
    ticker="MSFT",
    as_of="2026-01-02T10:00:00+00:00",
    providers=market.sources,
    source_references=references,
)
for status in ("running", "completed", "failed"):
    restored = RunStatus(
        run_id="abc123",
        tickers=["MSFT"],
        status=status,
        results={"MSFT": complete} if status != "running" else {},
    )
    assert restored.status == status

# Input normalization remains compatible for one-to-five ticker runs.
assert AnalysisRequest(tickers=["msft, aapl", "MSFT"]).normalized() == ["MSFT", "AAPL"]
assert len(AnalysisRequest(tickers=["A", "B", "C", "D", "E"]).normalized()) == 5

# Static hooks cover restoration, semantic disclosure, live announcements, and mobile cards.
root = Path(__file__).resolve().parents[1]
html = (root / "static" / "index.html").read_text(encoding="utf-8")
js = (root / "static" / "app.js").read_text(encoding="utf-8")
css = (root / "static" / "style.css").read_text(encoding="utf-8")
for token in ("overall-progress", 'role="status"', 'role="alert"', 'tabindex="-1"'):
    assert token in html
for token in ("localStorage", "?run", "aria-expanded", "aria-controls", "restoreSavedRun", "retryTicker"):
    if token == "?run":
        assert 'searchParams.set("run"' in js
    else:
        assert token in js
assert "onclick=" not in js
assert "prefers-reduced-motion" in css
assert "#summary-table td::before" in css
assert "@media (max-width: 900px)" in css
assert "overflow-x: hidden" in css
assert "scroll-padding-top" in css
assert "background-repeat: repeat, no-repeat, no-repeat" in css
assert "background: #090e19" in css
assert "grid-template-columns: minmax(0, 1fr) minmax(150px, 46%)" in css


# The existing status endpoint returns saved results and cleanly identifies stale IDs.
async def endpoint_checks() -> None:
    from app.config import settings
    from app.main import app
    from app.runs import Run, store

    original_db = settings.db_path
    settings.db_path = Path(tempfile.mkdtemp()) / "trust_ui.db"
    await store.init()
    assert "SourceReference" in app.openapi()["components"]["schemas"]
    run = Run(["MSFT"])
    run.status = "completed"
    run.completed_at = run.started_at + 2.5
    run.results = {"MSFT": complete}
    store.runs[run.run_id] = run
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/runs/{run.run_id}")
        assert response.status_code == 200
        assert response.json()["results"]["MSFT"]["as_of"]
        stale = await client.get("/api/runs/does-not-exist")
        assert stale.status_code == 404
    store.runs.pop(run.run_id, None)
    settings.db_path = original_db


asyncio.run(endpoint_checks())

print("TRUST UI CHECKS PASSED")
