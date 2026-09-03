"""In-memory run store with event fan-out for Server-Sent Events."""

import asyncio
import logging
import time
import uuid

from app.config import settings
from app.depth import DEFAULT_DEPTH, normalize_depth
from app.models import RunHistoryItem, RunStatus, StockAnalysis
from app.outlook import DEFAULT_OUTLOOK, normalize_outlook
from app import run_history
from app.workflow import analyze_ticker, fetch_portfolio_summary

logger = logging.getLogger("analysis")


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

    async def _execute(self, run: Run) -> None:
        try:
            semaphore = asyncio.Semaphore(settings.max_tickers)
            # One portfolio snapshot per run, shared by every ticker's manager
            # prompt and risk gate (instead of one fetch per ticker).
            portfolio_summary = await fetch_portfolio_summary()

            async def analyze_one(ticker: str) -> StockAnalysis:
                async with semaphore:
                    try:
                        return await analyze_ticker(
                            ticker,
                            run.emit,
                            portfolio_summary=portfolio_summary,
                            outlook=run.outlook,
                            depth=run.depth,
                        )
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
