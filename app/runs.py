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
        self.cancel_requested = False  # set once by RunStore.cancel; terminal
        self.ticker_tasks: dict[str, asyncio.Task[StockAnalysis]] = {}
        self.execution_task: asyncio.Task | None = None  # retained by create()

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
        # Which runs are currently awaiting each in-flight analysis, so cancel
        # only stops a shared analysis when this run is its last consumer.
        self.analysis_waiters: dict[AnalysisCacheKey, set[str]] = {}

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
        run.execution_task = asyncio.create_task(
            self._execute(run), name=f"execute:{run.run_id}"
        )
        return run

    async def cancel(self, run_id: str) -> RunStatus | None:
        """Stop a running run, keeping whatever ticker results already finished.

        Repeated cancels and cancels of finished runs are harmless no-ops that
        report the current status. Cancelling never revokes work an external
        provider already accepted; it stops this pipeline from advancing.
        """
        run = self.get(run_id)
        if run is None:
            return await run_history.get(run_id)
        if run.status != "running":
            return run.to_status()  # already terminal; a cancel cannot flip it
        run.cancel_requested = True
        run.status = "cancelled"
        run.completed_at = time.time()
        # Cancel the per-ticker tasks, then any in-flight shared analysis this
        # run is the sole waiter of (a joined analysis keeps serving others).
        for task in list(run.ticker_tasks.values()):
            task.cancel()
        for key, waiters in self.analysis_waiters.items():
            if run_id not in waiters or len(waiters) != 1:
                continue
            task = self.analysis_inflight.get(key)
            if task is not None and not task.done():
                task.cancel()
                self.analysis_inflight.pop(key, None)
        return run.to_status()

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

    async def _await_inflight(
        self, run: Run, key: AnalysisCacheKey, task: asyncio.Task
    ) -> StockAnalysis:
        """Await a shared in-flight analysis, registering this run as a waiter.

        The waiter set is what RunStore.cancel consults: an in-flight analysis
        is only cancelled when the cancelling run is its last consumer.
        """
        waiters = self.analysis_waiters.setdefault(key, set())
        waiters.add(run.run_id)
        try:
            return await asyncio.shield(task)
        finally:
            waiters.discard(run.run_id)
            if not waiters:
                self.analysis_waiters.pop(key, None)
            if self.analysis_inflight.get(key) is task and task.done():
                self.analysis_inflight.pop(key, None)

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
            result = await self._await_inflight(run, key, task)
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
        result = await self._await_inflight(run, key, task)
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

            if not run.cancel_requested:
                async def analyze_one(ticker: str) -> StockAnalysis:
                    async with semaphore:
                        try:
                            result = await self._analyze_one(run, ticker, portfolio_summary)
                        except Exception as exc:  # noqa: BLE001 - last-resort guard
                            logger.error("[analysis] %s crashed: %s", ticker, exc)
                            result = StockAnalysis(ticker=ticker, error=str(exc)[:300])
                    # Publish per ticker, not only at the end: the manager chat and
                    # run-status polls can serve a finished ticker while the rest
                    # of the run is still going.
                    run.results[result.ticker] = result
                    return result

                run.ticker_tasks = {
                    ticker: asyncio.create_task(
                        analyze_one(ticker), name=f"run:{run.run_id}:{ticker}"
                    )
                    for ticker in run.tickers
                }
                results = await asyncio.gather(*run.ticker_tasks.values())
                run.results = {result.ticker: result for result in results}
                run.status = "completed"
        except asyncio.CancelledError:
            # RunStore.cancel stopped the child tasks, not this coroutine:
            # settle them so the partial results are final, then terminate.
            run.status = "cancelled"
            if run.ticker_tasks:
                await asyncio.gather(
                    *run.ticker_tasks.values(), return_exceptions=True
                )
        except Exception as exc:  # noqa: BLE001 - preserve an interrupted run
            run.status = "failed"
            run.error = str(exc)[:300]
            logger.error("[analysis] run %s failed: %s", run.run_id, exc)
        if run.cancel_requested:
            # A cancel accepted at any point wins, even a hair after the
            # gather returned: a cancelled run can never flip to completed.
            run.status = "cancelled"
            run.error = None
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
