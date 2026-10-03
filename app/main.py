"""BetterTradingAgents - FastAPI application."""

import asyncio
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import accuracy, automation, broker, calibration, chat, memory, settings_store
from app.config import settings
from app.discovery import discover_stocks
from app.models import (
    AccuracyReport,
    AnalysisRequest,
    AnalysisResponse,
    BrokerAccount,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerPosition,
    BrokerStatus,
    CalibrationTrackRecord,
    AutomationStatus,
    AutomationUpdateRequest,
    CancelRunResponse,
    ClearHistoryResponse,
    ManagerChatRequest,
    ManagerChatResponse,
    RunHistoryItem,
    RunStatus,
    SettingsUpdateRequest,
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
async def static_revalidate(request, call_next):
    """Browsers revalidate assets and pages instead of trusting heuristics.

    The ES modules import each other (version-stamped once at the rename),
    and a stale cached constants.js once showed the old agent list after an
    update. no-cache keeps assets cached but forces a cheap 304 check every
    load, so code changes reach the browser on the next refresh.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


def _origin_authority(origin: str) -> str:
    """The host[:port] part of an Origin header, lowercased."""
    return urlparse(origin.lower()).netloc


@app.middleware("http")
async def local_only_guard(request, call_next):
    """Drive-by protection for a localhost app (settings hold real API keys).

    A malicious page in the user's browser can reach http://127.0.0.1:8000
    even though nothing is exposed to the network: DNS rebinding makes an
    attacker site resolve here, and cross-site form/fetch POSTs carry an
    Origin header naming the attacker. So: the Host header must be loopback,
    and any Origin must be this app's own origin. Same-origin fetches,
    same-page navigation, and header-less curl are unaffected.
    """
    host = (request.headers.get("host") or "").lower()
    if not host:
        return PlainTextResponse("forbidden: missing Host header", status_code=403)
    hostname = host[host.index("[") + 1 : host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
    if hostname not in ("127.0.0.1", "localhost", "::1"):
        return PlainTextResponse("forbidden: non-loopback Host", status_code=403)
    origin = request.headers.get("origin")
    if origin and _origin_authority(origin) != host:
        return PlainTextResponse("forbidden: cross-origin request", status_code=403)
    return await call_next(request)


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
        "/settings",
        "/portfolio",
        "/history",
        "/accuracy",
    ):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.on_event("startup")
async def startup() -> None:
    await memory.init()
    await broker.init()
    await store.init()
    await automation.init()
    # Keychain/DB overrides must land on the settings object before the
    # automation loop and anything else first reads them.
    await asyncio.to_thread(settings_store.apply_overrides)
    automation.start()
    mode = "mock (no LLM_API_KEY)" if not settings.llm_configured else settings.llm_model
    logger.info("[startup] BetterTradingAgents ready | llm=%s", mode)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/settings")
async def settings_page():
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/portfolio")
async def portfolio_page():
    return FileResponse(STATIC_DIR / "portfolio.html")


@app.get("/history")
async def history_page():
    return FileResponse(STATIC_DIR / "history.html")


@app.get("/accuracy")
async def accuracy_page():
    return FileResponse(STATIC_DIR / "accuracy.html")


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


@app.get("/api/settings")
async def get_settings():
    """Settings page payload. Secret values never leave the server: each
    secret carries only a configured flag and where it is stored from."""
    return await asyncio.to_thread(settings_store.snapshot)


@app.post("/api/settings")
async def update_settings(update: SettingsUpdateRequest):
    """Validate, persist, and live-apply a settings change. No restart: every
    consumer reads settings at call time. Secrets save to the OS keychain."""
    try:
        await asyncio.to_thread(settings_store.save, update.values, update.clear_secrets)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "[settings] updated: %s; cleared: %s",
        ", ".join(sorted(update.values)) or "nothing",
        ", ".join(update.clear_secrets) or "nothing",
    )
    return await asyncio.to_thread(settings_store.snapshot)


@app.post("/api/settings/reset")
async def reset_settings():
    """Drop every app-saved override and restore the .env values."""
    await asyncio.to_thread(settings_store.reset)
    logger.info("[settings] reset to .env defaults")
    return await asyncio.to_thread(settings_store.snapshot)


@app.get("/api/accuracy", response_model=AccuracyReport)
async def get_accuracy():
    """Every past call graded against realized performance (P1.9): rows,
    per-decision aggregates, and how many calls are still pending."""
    return await accuracy.accuracy_report()


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


@app.get("/api/automation", response_model=AutomationStatus)
async def automation_status():
    """Autopilot state, config, and recent sessions (portfolio page card)."""
    return await automation.status_snapshot()


@app.post("/api/automation", response_model=AutomationStatus)
async def set_automation(request: AutomationUpdateRequest):
    await automation.set_enabled(request.enabled)
    return await automation.status_snapshot()


@app.post("/api/automation/run")
async def automation_run_now():
    """Fire one autopilot session immediately (allowed while the market is
    closed; those orders queue for the next open)."""
    started = await automation.start_manual_session()
    if not started:
        raise HTTPException(status_code=409, detail="a session is already running")
    return {"started": True}
