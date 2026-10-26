"""BetterTradingAgents - FastAPI application."""

import asyncio
import ipaddress
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import accuracy, automation, broker, calibration, chat, memory, pages, settings_store, watchlist
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
    BrokerSyncState,
    CalibrationTrackRecord,
    AutomationStatus,
    AutomationUpdateRequest,
    CancelRunResponse,
    ClearHistoryResponse,
    EquityMark,
    ManagerChatRequest,
    ManagerChatResponse,
    RunHistoryItem,
    RunStatus,
    SettingsUpdateRequest,
    WatchlistAddRequest,
    WatchlistAddResponse,
    WatchlistItem,
    WatchlistUpdateRequest,
)
from app.outlook import DEFAULT_OUTLOOK, Outlook
from app.runs import store
from app.tools.market_data import get_closes_between

# Quiet terminal by default (TERMINAL_LOGGING=1 in .env for the full trail):
# warnings and errors always surface, INFO chatter does not.
logging.basicConfig(
    level=logging.INFO if settings.terminal_logging else logging.WARNING,
    format="%(message)s",
)
logger = logging.getLogger("analysis")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

app = FastAPI(title="BetterTradingAgents")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _origin_authority(origin: str) -> str:
    """The host[:port] part of an Origin header, lowercased."""
    return urlparse(origin.lower()).netloc


def _peer_is_remote(peer: str | None) -> bool:
    """True only for a parseable non-loopback IP (P2.11).

    Real sockets always present the peer as an IP string, so this is the one
    check a client cannot spoof by writing headers. Non-IP peers (None, or
    the synthetic names test harnesses use) cannot be judged and fall
    through to the Host/Origin checks below.
    """
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not address.is_loopback


@app.middleware("http")
async def local_only_guard(request, call_next):
    """Drive-by protection for a localhost app (settings hold real API keys).

    A malicious page in the user's browser can reach http://127.0.0.1:8000
    even though nothing is exposed to the network: DNS rebinding makes an
    attacker site resolve here, and cross-site form/fetch POSTs carry an
    Origin header naming the attacker. So: the socket peer must be loopback
    (unspoofable, and the real boundary while the server binds 127.0.0.1),
    the Host header must be loopback, and any Origin must be this app's own
    origin. Same-origin fetches, same-page navigation, and header-less curl
    are unaffected.
    """
    peer = request.client.host if request.client else None
    if _peer_is_remote(peer):
        return PlainTextResponse("forbidden: non-loopback client", status_code=403)
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
    """Every response revalidates: no stale UI mixes, no manual version bumps.

    Static files carry ETag/Last-Modified but would otherwise be heuristic
    cached; no-cache forces a cheap revalidation (unchanged files still 304),
    so a code change reaches the browser on the next refresh. Pages are
    server-rendered (app/pages.py), and the old ?v= asset stamps are gone:
    ETags already key every URL to its current bytes.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.on_event("startup")
async def startup() -> None:
    await memory.init()
    await watchlist.init()
    await broker.init()
    from app import overlay

    await overlay.init()  # item 13: stop/cooldown state table
    await store.init()
    await automation.init()
    # Keychain/DB overrides must land on the settings object before the
    # automation loop and anything else first reads them.
    await asyncio.to_thread(settings_store.apply_overrides)
    # P2.3: settle rows a crashed process left pending/unknown, and start the
    # interval reconciliation sweep, BEFORE anything can submit new orders.
    await broker.reconcile_startup()
    broker.start_sync_loop()
    memory.start()  # P1.17: settle matured decisions on a schedule
    automation.start()
    mode = "mock (no LLM_API_KEY)" if not settings.llm_configured else settings.llm_model
    logger.info("[startup] BetterTradingAgents ready | llm=%s", mode)


def _page(filename: str, active: str):
    """Render one page shell: shared nav, live feature toggles, current page."""
    return pages.render_page(
        filename,
        active,
        {key: _feature_enabled(key) for key, _label, _href in pages.FEATURE_LINKS},
    )


@app.get("/")
async def index():
    return _page("index.html", "home")


@app.get("/settings")
async def settings_page():
    return _page("settings.html", "settings")


@app.get("/portfolio")
async def portfolio_page():
    return _page("portfolio.html", "trades")


@app.get("/history")
async def history_page():
    return _page("history.html", "history")


def _feature_enabled(name: str) -> bool:
    """A retired/experimental UI is served only when experimentation is on
    AND its own toggle is on; both are editable live on the Settings page."""
    return settings.experimental_features and bool(
        getattr(settings, f"feature_{name}", False)
    )


@app.get("/api/features")
async def features():
    return {
        "experimental": settings.experimental_features,
        "accuracy": _feature_enabled("accuracy"),
        "compare": _feature_enabled("compare"),
        "watchlist": _feature_enabled("watchlist"),
    }


@app.get("/accuracy")
async def accuracy_page():
    if not _feature_enabled("accuracy"):
        raise HTTPException(status_code=404, detail="the accuracy page is disabled")
    return _page("accuracy.html", "accuracy")


@app.get("/watchlist")
async def watchlist_page():
    if not _feature_enabled("watchlist"):
        raise HTTPException(status_code=404, detail="the watchlist page is disabled")
    return _page("watchlist.html", "watchlist")


@app.get("/compare")
async def compare_page():
    if not _feature_enabled("compare"):
        raise HTTPException(status_code=404, detail="the compare page is disabled")
    return _page("compare.html", "compare")


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
            "forecast": ("bako" if settings.bako_api_key else "local"),
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
    """Every past call graded against realized performance (experimental):
    rows, per-decision aggregates, and how many calls are still pending."""
    if not _feature_enabled("accuracy"):
        raise HTTPException(status_code=404, detail="the accuracy report is disabled")
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
        tickers,
        # Incognito runs are unattributed: the client id is ignored so the
        # run cannot be listed, restored, or tied to this browser later.
        "" if request.incognito else (request.client_id or ""),
        request.outlook,
        request.depth,
        incognito=request.incognito,
    )
    logger.info(
        "[analysis] run %s started: %s (%s, %s%s)",
        run.run_id,
        "incognito tickers unnamed" if run.incognito else ", ".join(tickers),
        run.outlook,
        run.depth,
        ", incognito" if run.incognito else "",
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


async def _require_run_owner(run_id: str, client_id: str | None) -> None:
    """P2.11: one local tab must not read, cancel, or chat about another
    tab's run. Foreign runs answer the same 404 as missing ones, so the
    check never confirms a run's existence; ownerless runs (created before
    ownership, or by clients that never sent an id) stay open - the local
    guard already bounds who can reach the app at all."""
    owner = await store.owner_of(run_id)
    if owner and owner != (client_id or ""):
        raise HTTPException(status_code=404, detail="run not found")


@app.get("/api/runs/{run_id}", response_model=RunStatus)
async def run_status(
    run_id: str,
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    await _require_run_owner(run_id, client_id)
    status = await store.get_status(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail="run not found")
    return status


@app.post("/api/runs/{run_id}/discard")
async def discard_run(run_id: str):
    """Forget an incognito run when its page closes.

    Incognito runs were never persisted; this drops the in-memory copy (and
    cancels it if still running) so no report data outlives the tab. Normal
    runs are history and cannot be discarded.
    """
    if store.get(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    if not await store.discard(run_id):
        raise HTTPException(status_code=400, detail="only incognito runs can be discarded")
    logger.info("[analysis] incognito run %s discarded", run_id)
    return {"discarded": True}


@app.post("/api/runs/{run_id}/cancel", response_model=CancelRunResponse)
async def cancel_run(
    run_id: str,
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    """Cancel a running analysis, preserving finished ticker results.

    Repeated requests are harmless and return the run's current status; a
    cancelled run never flips back to completed. Cancelling stops this
    pipeline from advancing but cannot revoke an LLM request an external
    provider has already accepted.
    """
    await _require_run_owner(run_id, client_id)
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
            # P2.8: replay from a snapshot, not the live list. Iterating
            # run.events directly re-picked up events appended while this
            # generator was suspended at a yield, and the queue then delivered
            # the same event a second time.
            replay = list(run.events)
            replayed = {id(event) for event in replay}
            for event in replay:
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
async def manager_chat(
    run_id: str,
    request: ManagerChatRequest,
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    """Follow-up Q&A with the portfolio manager about one ticker of a run."""
    ticker = request.ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="invalid ticker symbol")
    await _require_run_owner(run_id, client_id)
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
    # Incognito chat stays incognito: the dossier is built without a portfolio.
    run = store.get(run_id)
    try:
        answer = await chat.answer_question(
            analysis,
            [message.model_dump() for message in request.messages],
            incognito=bool(run and run.incognito),
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


@app.get("/api/broker/sync", response_model=BrokerSyncState)
async def broker_sync_state():
    """Reconciliation state for the portfolio page's out-of-sync banner
    (P2.3); a stored result staler than a minute is refreshed once, so
    polling tabs reach the broker at most once a minute."""
    state = await broker.sync_state(max_age_s=60.0)
    return BrokerSyncState(**state)


@app.get("/api/broker/account", response_model=BrokerAccount)
async def broker_account():
    try:
        return await broker.account()
    except (broker.BrokerNotConfigured, broker.BrokerRuleError) as exc:
        raise _broker_http_error(exc) from exc


@app.get("/api/broker/equity-mark", response_model=EquityMark)
async def broker_equity_mark():
    """The drawdown brake's baseline (P2.13); pure local read, no broker call."""
    return await broker.equity_mark()


@app.post("/api/broker/equity-mark/reset", response_model=EquityMark)
async def broker_reset_equity_mark():
    """Re-baseline the drawdown brake to current equity after a deposit or
    withdrawal (P2.13); the note records why the baseline moved."""
    try:
        return await broker.reset_equity_mark()
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


def _require_client(client_id: str | None) -> str:
    if client_id is None or not CLIENT_ID_RE.match(client_id):
        raise HTTPException(status_code=400, detail="invalid client id")
    return client_id


def _require_feature(name: str) -> None:
    if not _feature_enabled(name):
        raise HTTPException(status_code=404, detail=f"the {name} feature is disabled")


@app.get("/api/watchlist", response_model=list[WatchlistItem])
async def get_watchlist(
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    _require_feature("watchlist")
    if client_id is None:
        return []
    owner = _require_client(client_id)
    return await watchlist.list_watchlist(owner)


@app.post("/api/watchlist", response_model=WatchlistAddResponse)
async def add_watchlist(
    request: WatchlistAddRequest,
    client_id: str | None = Header(default=None, alias="X-Client-ID"),
):
    _require_feature("watchlist")
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
    _require_feature("watchlist")
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
    _require_feature("watchlist")
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
    _require_feature("watchlist")
    owner = _require_client(client_id)
    return {"deleted": await watchlist.clear(owner)}
