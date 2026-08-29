"""In-memory run store with event fan-out for Server-Sent Events."""

import asyncio
import logging
import time
import uuid

from app.config import settings
from app.models import RunStatus, StockAnalysis
from app.workflow import analyze_ticker

logger = logging.getLogger("analysis")


class Run:
    def __init__(self, tickers: list[str]):
        self.run_id = uuid.uuid4().hex[:12]
        self.tickers = tickers
        self.status = "running"
        self.mock_mode = not settings.llm_configured
        self.started_at = time.time()
        self.completed_at: float | None = None
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
            status=self.status,
            mock_mode=self.mock_mode,
            started_at=self.started_at,
            duration_s=round(finished - self.started_at, 1),
            results=self.results,
        )


class RunStore:
    """Holds all runs; only completed results survive here (no DB - by design)."""

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}

    def create(self, tickers: list[str]) -> Run:
        run = Run(tickers)
        self.runs[run.run_id] = run
        asyncio.create_task(self._execute(run))
        return run

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    async def _execute(self, run: Run) -> None:
        semaphore = asyncio.Semaphore(settings.max_tickers)

        async def analyze_one(ticker: str) -> StockAnalysis:
            async with semaphore:
                try:
                    return await analyze_ticker(ticker, run.emit)
                except Exception as exc:  # noqa: BLE001 - last-resort guard
                    logger.error("[analysis] %s crashed: %s", ticker, exc)
                    return StockAnalysis(ticker=ticker, error=str(exc)[:300])

        results = await asyncio.gather(*(analyze_one(t) for t in run.tickers))
        run.results = {result.ticker: result for result in results}
        run.status = "completed"
        run.completed_at = time.time()
        await run.emit(
            "analysis_completed",
            {
                "run_id": run.run_id,
                "duration_s": round(time.time() - run.started_at, 1),
            },
        )


store = RunStore()
