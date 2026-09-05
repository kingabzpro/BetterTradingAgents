"""In-memory run store with event fan-out for Server-Sent Events."""

import asyncio
import logging
import time
import uuid

from app.config import settings
from app.depth import DEFAULT_DEPTH, normalize_depth
from app.models import PortfolioSummary, RunHistoryItem, RunStatus, StockAnalysis
from app.outlook import DEFAULT_OUTLOOK, normalize_outlook
from app import run_history
from app.workflow import analyze_ticker, fetch_portfolio_summary

logger = logging.getLogger("analysis")
ANALYSIS_CACHE_TTL_SECONDS = 60 * 60

AnalysisCacheKey = tuple[str, str, str, int, bool, str]


class Run:
    def __init__(
        self,
        tickers: list[str],
        client_id: str = "",
        outlook: str = DEFAULT_OUTLOOK,
        depth: str = DEFAULT_DEPTH,
    ):
        self.run_id = uuid.uuid4().hex[:12]
        self.tickers = tickers
        self.client_id = client_id
        self.outlook = normalize_outlook(outlook)
        self.depth = normalize_depth(depth)
        self.status = "running"
        self.mock_mode = not settings.llm_configured
        self.started_at = time.time()
        self.completed_at: float | None = None
        self.error: str | None = None
        self.events: list[dict] = []
        self.queues: list[asyncio.Queue] = []
        self.results: dict[str, StockAnalysis] = {}

    async def emit(self, event_type: str, payload: dict) -> None:
        event = {"type": event_type, **payload}
        # Token streams are live-only (ROADMAP 3.2): reconnect replays would
        # re-send thousands of cosmetic chunks, and the final text already
        # arrives with agent_completed. Everything else is replayable.
        if event_type != "agent_token":
            self.events.append(event)
        for queue in list(self.queues):
            queue.put_nowait(event)

    def to_status(self) -> RunStatus:
        finished = self.completed_at or time.time()
        return RunStatus(
            run_id=self.run_id,
            tickers=self.tickers,
            outlook=self.outlook,
            depth=self.depth,
            status=self.status,
            mock_mode=self.mock_mode,
            started_at=self.started_at,
            duration_s=round(finished - self.started_at, 1),
            error=self.error,
            results=self.results,
        )


class RunStore:
    """Coordinates active runs in memory and completed runs in SQLite."""

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.analysis_cache: dict[
            AnalysisCacheKey, tuple[float, StockAnalysis]
        ] = {}
        self.analysis_inflight: dict[
            AnalysisCacheKey, asyncio.Task[StockAnalysis]
        ] = {}

    async def init(self) -> None:
        await run_history.init()

    async def create(
        self,
        tickers: list[str],
        client_id: str = "",
        outlook: str = DEFAULT_OUTLOOK,
        depth: str = DEFAULT_DEPTH,
    ) -> Run:
        run = Run(tickers, client_id, outlook, depth)
        self.runs[run.run_id] = run
        await self._persist(run)
        asyncio.create_task(self._execute(run))
        return run

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    async def get_status(self, run_id: str) -> RunStatus | None:
        run = self.get(run_id)
        return run.to_status() if run is not None else await run_history.get(run_id)

    async def list_history(
        self, client_id: str, limit: int = 50
    ) -> list[RunHistoryItem]:
        return await run_history.list_runs(client_id, limit)

    async def clear_history(self, client_id: str) -> int:
        removed_ids = set(await run_history.clear(client_id))
        removed_ids.update(
            run_id
            for run_id, run in self.runs.items()
            if run.client_id == client_id and run.status != "running"
        )
        for run_id in removed_ids:
            self.runs.pop(run_id, None)
        return len(removed_ids)

    async def _persist(self, run: Run) -> None:
        try:
            await run_history.save(
                run.to_status(), completed_at=run.completed_at, owner_id=run.client_id
            )
        except Exception as exc:  # noqa: BLE001 - analysis must survive DB trouble
            logger.error("[history] could not save run %s: %s", run.run_id, exc)

    @staticmethod
    def _cache_key(
        run: Run, ticker: str, portfolio: PortfolioSummary | None
    ) -> AnalysisCacheKey:
        return (
            ticker.upper(),
            run.outlook,
            run.depth,
            settings.debate_rounds,
            run.mock_mode,
            portfolio.model_dump_json() if portfolio is not None else "unavailable",
        )

    @staticmethod
    async def _emit_cached(run: Run, result: StockAnalysis) -> None:
        await run.emit(
            "ticker_started", {"ticker": result.ticker, "cached": True}
        )
        await run.emit(
            "ticker_data",
            {
                "ticker": result.ticker,
                "price": result.price,
                "company_name": result.company_name,
                "sources": result.providers,
                "cached": True,
            },
        )
        await run.emit(
            "ticker_completed",
            {
                "ticker": result.ticker,
                "decision": result.decision,
                "confidence": result.confidence,
                "duration_s": 0.0,
                "analysis": result.model_dump(),
                "cached": True,
            },
        )

    async def _analyze_one(
        self,
        run: Run,
        ticker: str,
        portfolio: PortfolioSummary | None,
    ) -> StockAnalysis:
        key = self._cache_key(run, ticker, portfolio)
        cached = self.analysis_cache.get(key)
        if cached and time.monotonic() - cached[0] < ANALYSIS_CACHE_TTL_SECONDS:
            result = cached[1].model_copy(deep=True)
            logger.info("[analysis] %s cache hit", ticker)
            await self._emit_cached(run, result)
            return result

        task = self.analysis_inflight.get(key)
        if task is not None:
            try:
                result = await asyncio.shield(task)
            finally:
                if self.analysis_inflight.get(key) is task and task.done():
                    self.analysis_inflight.pop(key, None)
            if not result.error:
                self.analysis_cache[key] = (
                    time.monotonic(),
                    result.model_copy(deep=True),
                )
            result = result.model_copy(deep=True)
            logger.info("[analysis] %s joined cached in-flight analysis", ticker)
            await self._emit_cached(run, result)
            return result

        task = asyncio.create_task(
            analyze_ticker(
                ticker,
                run.emit,
                portfolio_summary=portfolio,
                outlook=run.outlook,
                depth=run.depth,
                run_id=run.run_id,
            )
        )
        self.analysis_inflight[key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if self.analysis_inflight.get(key) is task and task.done():
                self.analysis_inflight.pop(key, None)
        if not result.error:
            self.analysis_cache[key] = (
                time.monotonic(),
                result.model_copy(deep=True),
            )
        return result

    async def _execute(self, run: Run) -> None:
        try:
            semaphore = asyncio.Semaphore(settings.max_tickers)
            # One portfolio snapshot per run, shared by every ticker's manager
            # prompt and risk gate (instead of one fetch per ticker).
            portfolio_summary = await fetch_portfolio_summary()

            async def analyze_one(ticker: str) -> StockAnalysis:
                async with semaphore:
                    try:
                        return await self._analyze_one(run, ticker, portfolio_summary)
                    except Exception as exc:  # noqa: BLE001 - last-resort guard
                        logger.error("[analysis] %s crashed: %s", ticker, exc)
                        return StockAnalysis(ticker=ticker, error=str(exc)[:300])

            results = await asyncio.gather(*(analyze_one(t) for t in run.tickers))
            run.results = {result.ticker: result for result in results}
            run.status = "completed"
        except Exception as exc:  # noqa: BLE001 - preserve an interrupted run
            run.status = "failed"
            run.error = str(exc)[:300]
            logger.error("[analysis] run %s failed: %s", run.run_id, exc)
        run.completed_at = time.time()
        await self._persist(run)
        await run.emit(
            "analysis_completed",
            {
                "run_id": run.run_id,
                "duration_s": round(time.time() - run.started_at, 1),
                "status": run.status,
                "error": run.error,
            },
        )


store = RunStore()
