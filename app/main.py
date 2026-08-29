"""BetterTradingAgents - FastAPI application."""

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import portfolio
from app.config import settings
from app.models import AnalysisRequest, AnalysisResponse, PortfolioAddRequest
from app.runs import store

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("analysis")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")

app = FastAPI(title="BetterTradingAgents")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup() -> None:
    await portfolio.init()
    mode = "mock (no LLM_API_KEY)" if not settings.llm_configured else settings.llm_model
    logger.info("[startup] BetterTradingAgents ready | llm=%s", mode)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/portfolio")
async def portfolio_page():
    return FileResponse(STATIC_DIR / "portfolio.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "llm_configured": settings.llm_configured,
        "llm_model": settings.llm_model if settings.llm_configured else None,
        "mock_mode": not settings.llm_configured,
        "providers": {
            "prices": "yfinance",
            "fundamentals": "finnhub" if settings.finnhub_api_key else "yfinance",
            "news_search": "olostep" if settings.olostep_api_key else "disabled",
        },
        "max_tickers": settings.max_tickers,
    }


@app.post("/api/analyze", response_model=AnalysisResponse)
async def analyze(request: AnalysisRequest):
    tickers = request.normalized()
    if not tickers:
        raise HTTPException(status_code=400, detail="no valid tickers provided")
    if len(tickers) > settings.max_tickers:
        raise HTTPException(
            status_code=400,
            detail=f"too many tickers: max {settings.max_tickers} at once",
        )
    invalid = [t for t in tickers if not TICKER_RE.match(t)]
    if invalid:
        raise HTTPException(
            status_code=400, detail=f"invalid ticker symbol(s): {', '.join(invalid)}"
        )
    run = store.create(tickers)
    logger.info(
        "[analysis] run %s started: %s", run.run_id, ", ".join(tickers)
    )
    return AnalysisResponse(run_id=run.run_id, tickers=tickers)


@app.get("/api/runs/{run_id}")
async def run_status(run_id: str):
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run.to_status()


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    async def stream():
        queue: asyncio.Queue = asyncio.Queue()
        run.queues.append(queue)
        try:
            replayed = {id(event) for event in run.events}
            for event in run.events:
                yield _sse(event)
            while True:
                event = await queue.get()
                if id(event) in replayed:
                    continue
                yield _sse(event)
                if event["type"] == "analysis_completed":
                    break
        finally:
            if queue in run.queues:
                run.queues.remove(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@app.get("/api/portfolio")
async def get_portfolio():
    return await portfolio.get_portfolio()


@app.post("/api/portfolio/add")
async def add_position(request: PortfolioAddRequest):
    try:
        position = await portfolio.add_position(
            request.ticker, request.quantity, request.entry_price
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return position
