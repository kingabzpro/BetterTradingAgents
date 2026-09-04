"""BetterTradingAgents - FastAPI application."""

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import portfolio
from app.config import settings
from app.discovery import discover_stocks
from app.models import (
    AnalysisRequest,
    AnalysisResponse,
    ClearHistoryResponse,
    PortfolioAddRequest,
    PortfolioCloseRequest,
    RunHistoryItem,
    RunStatus,
)
from app.outlook import DEFAULT_OUTLOOK, Outlook
from app.runs import store

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("analysis")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

app = FastAPI(title="BetterTradingAgents")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup() -> None:
    await portfolio.init()
    await store.init()
    mode = "mock (no LLM_API_KEY)" if not settings.llm_configured else settings.llm_model
    logger.info("[startup] BetterTradingAgents ready | llm=%s", mode)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/portfolio")
async def portfolio_page():
    return FileResponse(STATIC_DIR / "portfolio.html")


@app.get("/history")
async def history_page():
    return FileResponse(STATIC_DIR / "history.html")


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
            "forecast": "timegpt" if settings.nixtla_api_key else "local",
        },
        "max_tickers": settings.max_tickers,
        "debate_rounds": settings.debate_rounds,
    }


@app.get("/api/discover")
async def discover(outlook: Outlook = Query(default=DEFAULT_OUTLOOK)):
    try:
        return await discover_stocks(outlook, min(5, settings.max_tickers))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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
    run = await store.create(
        tickers, request.client_id or "", request.outlook, request.depth
    )
    logger.info(
        "[analysis] run %s started: %s (%s, %s)",
        run.run_id,
        ", ".join(tickers),
        run.outlook,
        run.depth,
    )
    return AnalysisResponse(run_id=run.run_id, tickers=tickers)


@app.get("/api/runs", response_model=list[RunHistoryItem])
async def run_history(
    limit: int = Query(default=50, ge=1, le=100),
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    if client_id is None:
        return []
    if not CLIENT_ID_RE.match(client_id):
        raise HTTPException(status_code=400, detail="invalid client id")
    return await store.list_history(client_id, limit)


@app.delete("/api/runs", response_model=ClearHistoryResponse)
async def clear_run_history(
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    if client_id is None or not CLIENT_ID_RE.match(client_id):
        raise HTTPException(status_code=400, detail="invalid client id")
    return ClearHistoryResponse(deleted=await store.clear_history(client_id))


@app.get("/api/runs/{run_id}", response_model=RunStatus)
async def run_status(run_id: str):
    status = await store.get_status(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail="run not found")
    return status


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


@app.post("/api/portfolio/close")
async def close_position(request: PortfolioCloseRequest):
    try:
        position = await portfolio.close_position(
            request.position_id, request.exit_price
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return position
