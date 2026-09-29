"""BetterTradingAgents - FastAPI application."""

import asyncio
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import broker, calibration, chat, memory, portfolio, watchlist
from app.config import settings
from app.discovery import discover_stocks
from app.models import (
    AnalysisRequest,
    AnalysisResponse,
    BrokerAccount,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerPosition,
    BrokerReplayRequest,
    BrokerStatus,
    CalibrationTrackRecord,
    CancelRunResponse,
    ClearHistoryResponse,
    ManagerChatRequest,
    ManagerChatResponse,
    PortfolioAddRequest,
    PortfolioCloseRequest,
    PortfolioImportRequest,
    PortfolioImportResponse,
    RunHistoryItem,
    RunStatus,
    WatchlistAddRequest,
    WatchlistAddResponse,
    WatchlistItem,
    WatchlistUpdateRequest,
)
from app.outlook import DEFAULT_OUTLOOK, Outlook
from app.runs import store
from app.tools.market_data import get_closes_between

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("analysis")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

app = FastAPI(title="BetterTradingAgents")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def revalidate_assets(request, call_next):
    """Never let the browser serve a stale UI mix (old CSS + new markup).

    Static files only carry ETag/Last-Modified, so browsers heuristic-cache
    them and may skip revalidation entirely; since the files change in place,
    that can pair a stale stylesheet with fresh HTML - visibly broken layout.
    "no-cache" means revalidate before use (unchanged files still 304), and
    the ?v= bumps on asset URLs then always resolve to a consistent set.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/static/", "/api/")) or path in (
        "/",
        "/portfolio",
        "/history",
        "/watchlist",
        "/compare",
    ):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.on_event("startup")
async def startup() -> None:
    await portfolio.init()
    await memory.init()
    await watchlist.init()
    await broker.init()
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


@app.get("/watchlist")
async def watchlist_page():
    return FileResponse(STATIC_DIR / "watchlist.html")


@app.get("/compare")
async def compare_page():
    return FileResponse(STATIC_DIR / "compare.html")


@app.get("/trading")
async def trading_page():
    """The paper view merged into the portfolio page (P2.1); keep old links working."""
    return RedirectResponse("/portfolio", status_code=307)


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
            "social": "olostep" if settings.olostep_api_key else "disabled",
            "forecast": "timegpt" if settings.nixtla_api_key else "local",
        },
        "max_tickers": settings.max_tickers,
        "debate_rounds": settings.debate_rounds,
    }


@app.get("/api/calibration", response_model=CalibrationTrackRecord)
async def calibration_lookup(
    decision: str = Query(pattern="^(BUY|HOLD|SELL)$"),
    confidence: float = Query(ge=0.0, le=1.0),
    outlook: str = Query(default="", max_length=32),
    depth: str = Query(default="", max_length=32),
):
    """Historical outcome rates for one decision + evidence bucket (P1.1).

    Rates only exist once enough mature graded decisions share the scope;
    otherwise available=false with the sample size, so the UI shows
    "Track record unavailable" instead of a number a handful of outcomes
    cannot support. Confidence stays evidence strength: no screen may
    present it as a probability of profit.
    """
    record = await asyncio.to_thread(
        calibration.track_record, decision, confidence, outlook, depth
    )
    return CalibrationTrackRecord(
        decision=decision,
        confidence_bucket=record["confidence_bucket"],
        n_mature=record["n"],
        min_observations=record["min_observations"],
        available=record["available"],
        directional_hit_rate=record.get("directional_hit_rate"),
        positive_alpha_rate=record.get("positive_alpha_rate"),
        mean_alpha_pct=record.get("mean_alpha_pct"),
        median_alpha_pct=record.get("median_alpha_pct"),
        missed_upside_rate=record.get("missed_upside_rate"),
        avoided_downside_rate=record.get("avoided_downside_rate"),
        mean_realized_pct=record.get("mean_realized_pct"),
        models_pooled=record.get("models_pooled", 0),
    )


@app.get("/api/discover")
async def discover(outlook: Outlook = Query(default=DEFAULT_OUTLOOK)):
    try:
        return await discover_stocks(outlook, min(5, settings.max_tickers))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/price-history/{ticker}")
async def price_history(ticker: str):
    """Six months of daily closes for the price-context chart (P1.5).

    Live market data only: an unavailable history returns empty lists and the
    UI says so, it never draws invented points.
    """
    ticker = ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="invalid ticker symbol")
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=186)
    closes = await get_closes_between(ticker, start.isoformat(), end.isoformat())
    return {"ticker": ticker, "dates": list(closes), "closes": list(closes.values())}


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


@app.post("/api/runs/{run_id}/cancel", response_model=CancelRunResponse)
async def cancel_run(run_id: str):
    """Cancel a running analysis, preserving finished ticker results.

    Repeated requests are harmless and return the run's current status; a
    cancelled run never flips back to completed. Cancelling stops this
    pipeline from advancing but cannot revoke an LLM request an external
    provider has already accepted.
    """
    status = await store.cancel(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail="run not found")
    return CancelRunResponse(run_id=status.run_id, status=status.status)


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


@app.post("/api/runs/{run_id}/chat", response_model=ManagerChatResponse)
async def manager_chat(run_id: str, request: ManagerChatRequest):
    """Follow-up Q&A with the portfolio manager about one ticker of a run."""
    ticker = request.ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="invalid ticker symbol")
    status = await store.get_status(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail="run not found")
    analysis = status.results.get(ticker)
    if analysis is None:
        raise HTTPException(status_code=404, detail=f"ticker {ticker} not in this run")
    if analysis.error:
        raise HTTPException(
            status_code=409,
            detail=f"analysis for {ticker} failed; there is nothing to discuss",
        )
    if request.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="last message must be from the user")
    try:
        answer = await chat.answer_question(
            analysis, [message.model_dump() for message in request.messages]
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a retryable 503
        raise HTTPException(status_code=503, detail=f"manager chat failed: {exc}") from exc
    return ManagerChatResponse(
        ticker=ticker, answer=answer, mock_mode=not settings.llm_configured
    )


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


@app.post("/api/portfolio/import", response_model=PortfolioImportResponse)
async def import_positions(request: PortfolioImportRequest):
    return await portfolio.import_positions(request.positions)


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


def _broker_http_error(exc: Exception) -> HTTPException:
    """Map broker-module exceptions to the repo's HTTPException conventions."""
    if isinstance(exc, broker.BrokerNotConfigured):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, broker.BrokerRuleError):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    return HTTPException(status_code=503, detail=f"paper trading unavailable: {exc}")


@app.get("/api/broker/status", response_model=BrokerStatus)
async def broker_status():
    return await broker.status()


@app.get("/api/broker/account", response_model=BrokerAccount)
async def broker_account():
    try:
        return await broker.account()
    except (broker.BrokerNotConfigured, broker.BrokerRuleError) as exc:
        raise _broker_http_error(exc) from exc


@app.get("/api/broker/positions", response_model=list[BrokerPosition])
async def broker_positions():
    try:
        return await broker.positions()
    except (broker.BrokerNotConfigured, broker.BrokerRuleError) as exc:
        raise _broker_http_error(exc) from exc


@app.post("/api/broker/orders", response_model=BrokerOrder)
async def place_broker_order(request: BrokerOrderRequest):
    """Place one paper order. A recommendation never auto-submits: confirm
    must be explicitly true, and every guard rail runs server-side."""
    ticker = request.ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="invalid ticker symbol")
    if not request.confirm:
        raise HTTPException(
            status_code=400, detail="order not confirmed; set confirm to true"
        )
    try:
        return await broker.submit_order(
            request.run_id, ticker, request.side, request.notional
        )
    except (broker.BrokerNotConfigured, broker.BrokerRuleError) as exc:
        raise _broker_http_error(exc) from exc


@app.get("/api/broker/orders", response_model=list[BrokerOrder])
async def list_broker_orders(limit: int = Query(default=50, ge=1, le=100)):
    try:
        return await broker.list_orders(limit)
    except (broker.BrokerNotConfigured, broker.BrokerRuleError) as exc:
        raise _broker_http_error(exc) from exc


@app.post("/api/broker/replay", response_model=BrokerOrder)
async def replay_broker_position(request: BrokerReplayRequest):
    """Replay one open local position into the paper account (origin=replay).

    Idempotent per position: the deterministic client_order_id is persisted
    before the POST, so repeated clicks or timeout retries never double-order.
    Skipped positions come back as errors with the reason; the caller reports
    them in a summary instead of dropping them silently.
    """
    try:
        return await broker.replay_position(request.position_id)
    except (broker.BrokerNotConfigured, broker.BrokerRuleError) as exc:
        raise _broker_http_error(exc) from exc


@app.get("/api/broker/equity")
async def broker_equity(period: str = Query(default="1M", pattern="^(1W|1M|3M|1A)$")):
    try:
        return await broker.equity(period)
    except (broker.BrokerNotConfigured, broker.BrokerRuleError) as exc:
        raise _broker_http_error(exc) from exc


@app.get("/api/broker/performance")
async def broker_performance(limit: int = Query(default=50, ge=1, le=100)):
    """Per filled paper order: return since fill and alpha vs SPY (P2.1 M5)."""
    try:
        return await broker.order_performance(limit)
    except (broker.BrokerNotConfigured, broker.BrokerRuleError) as exc:
        raise _broker_http_error(exc) from exc


@app.delete("/api/broker/orders/{client_order_id}", response_model=BrokerOrder)
async def cancel_broker_order(client_order_id: str):
    try:
        return await broker.cancel_order(client_order_id)
    except (broker.BrokerNotConfigured, broker.BrokerRuleError) as exc:
        raise _broker_http_error(exc) from exc


def _require_client(client_id: str | None) -> str:
    if client_id is None or not CLIENT_ID_RE.match(client_id):
        raise HTTPException(status_code=400, detail="invalid client id")
    return client_id


@app.get("/api/watchlist", response_model=list[WatchlistItem])
async def get_watchlist(
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    if client_id is None:
        return []
    owner = _require_client(client_id)
    return await watchlist.list_watchlist(owner)


@app.post("/api/watchlist", response_model=WatchlistAddResponse)
async def add_watchlist(
    request: WatchlistAddRequest,
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    owner = _require_client(client_id)
    try:
        item, already = await watchlist.add_item(
            owner,
            request.ticker,
            note=request.note,
            outlook=request.outlook,
            depth=request.depth,
            run_id=request.run_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return WatchlistAddResponse(item=item, already_watched=already)


@app.patch("/api/watchlist/{item_id}", response_model=WatchlistItem)
async def update_watchlist_item(
    item_id: int,
    request: WatchlistUpdateRequest,
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    owner = _require_client(client_id)
    try:
        return await watchlist.update_item(
            owner, item_id, request.note, request.outlook, request.depth
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/watchlist/{item_id}")
async def remove_watchlist_item(
    item_id: int,
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    owner = _require_client(client_id)
    try:
        ticker = await watchlist.remove_item(owner, item_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ticker": ticker, "removed": 1}


@app.delete("/api/watchlist")
async def clear_watchlist(
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    owner = _require_client(client_id)
    return {"deleted": await watchlist.clear(owner)}
