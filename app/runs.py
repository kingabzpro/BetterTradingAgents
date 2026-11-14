"""In-memory run store with event fan-out for Server-Sent Events."""

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict

from app.config import settings
from app.depth import DEFAULT_DEPTH, normalize_depth
from app.models import PortfolioSummary, RunHistoryItem, RunStatus, StockAnalysis
from app.outlook import DEFAULT_OUTLOOK, normalize_outlook
from app import changes, run_history
from app.redact import redact
from app.workflow import analyze_ticker, fetch_held_closes, fetch_portfolio_summary

logger = logging.getLogger("analysis")
ANALYSIS_CACHE_TTL_SECONDS = 60 * 60
# Eviction cap: the cache would otherwise grow for the life of the process.
ANALYSIS_CACHE_MAX_ENTRIES = 64
# How long a finished run stays in memory after persisting, so a browser tab
# whose SSE stream dropped mid-run can reconnect and replay its ending.
FINISHED_RUN_GRACE_SECONDS = 600

# Cache key: ticker, outlook, depth, debate rounds, mock mode, portfolio (or
# "unavailable"), incognito, then the sizing/gating settings that shape the
# result. Kept as a plain tuple so new inputs cannot silently miss the alias.
AnalysisCacheKey = tuple


def _portfolio_key(portfolio: PortfolioSummary | None) -> str:
    """A stable portfolio identity for the cache key (P2.10).

    Live price drift rewrites values, PnL, and equity on every call, which
    used to make the key unique per run and the cache unable to hit across
    runs. Only the holdings, the cash bucket, and the drawdown high-water
    bucket change what the manager and the risk gates decide, so the key
    carries those; buckets are $100 so a mark-to-market wiggle cannot split
    it. A real trade moves quantities/cash/high-water into another bucket.
    """
    if portfolio is None:
        return "unavailable"
    return json.dumps(
        {
            "holdings": sorted(
                (position.ticker, round(position.quantity or 0.0, 6))
                for position in portfolio.positions
            ),
            "cash_bucket": round(portfolio.cash / 100),
            "high_water_bucket": round((portfolio.equity_high_water or 0.0) / 100),
        },
        sort_keys=True,
    )


def _no_usable_output(result: StockAnalysis) -> bool:
    """True when neither the manager nor any researcher produced a result.

    Such a ticker carries only the default HOLD a dead manager falls back to,
    so a run where every ticker looks like this is an outage, not an answer.
    """
    if result.error is not None:
        return True
    if result.manager_decision is not None:
        return False
    return all(
        getattr(result, field) is None
        for field in (
            "technical", "fundamental", "news", "sentiment",
            "forecast", "market", "bull", "bear", "judge",
        )
    )


class Run:
    def __init__(
        self,
        tickers: list[str],
        client_id: str = "",
        outlook: str = DEFAULT_OUTLOOK,
        depth: str = DEFAULT_DEPTH,
        incognito: bool = False,
    ):
        self.run_id = uuid.uuid4().hex[:12]
        self.tickers = tickers
        self.client_id = client_id
        self.outlook = normalize_outlook(outlook)
        self.depth = normalize_depth(depth)
        self.status = "running"
        self.mock_mode = not settings.llm_configured
        # Incognito: market-only. No portfolio, no decision memory, no
        # persistence - the run lives in memory until its page closes.
        self.incognito = incognito
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
            incognito=self.incognito,
            started_at=self.started_at,
            duration_s=round(finished - self.started_at, 1),
            error=self.error,
            results=self.results,
        )


class RunStore:
    """Coordinates active runs in memory and completed runs in SQLite."""

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.analysis_cache: OrderedDict[
            AnalysisCacheKey, tuple[float, StockAnalysis]
        ] = OrderedDict()
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
        incognito: bool = False,
    ) -> Run:
        if incognito:
            self._prune_incognito()
        else:
            self._prune_finished()
        run = Run(tickers, client_id, outlook, depth, incognito=incognito)
        self.runs[run.run_id] = run
        if not incognito:
            await self._persist(run)
        run.execution_task = asyncio.create_task(
            self._execute(run), name=f"execute:{run.run_id}"
        )
        return run

    def _prune_incognito(self) -> None:
        """Keep at most two finished incognito runs in memory (older pages
        lose their chat, bounded memory); running ones always stay."""
        terminal = sorted(
            (run for run in self.runs.values() if run.incognito and run.status != "running"),
            key=lambda run: run.started_at,
        )
        for run in terminal[:-2]:
            self.runs.pop(run.run_id, None)

    async def discard(self, run_id: str) -> bool:
        """Forget an incognito run when its page closes: cancel it if still
        running, then drop it from memory so no report data outlives the tab
        (logs still note that some incognito run happened, never which ticker
        or call). Persisted runs are history and cannot be discarded."""
        run = self.get(run_id)
        if run is None or not run.incognito:
            return False
        if run.status == "running":
            await self.cancel(run_id)
        self.runs.pop(run_id, None)
        return True

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

    async def owner_of(self, run_id: str) -> str | None:
        """The owning client id, memory first then history; None when the
        run is unknown or predates ownership (P2.11)."""
        run = self.get(run_id)
        if run is not None:
            return run.client_id or None
        return await run_history.owner_of(run_id)

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
            tuple(
                (key, *settings.llm_for(key).values())
                for key in ("analysts", "debate", "manager", "market", "technical",
                            "fundamental", "news", "sentiment", "forecast", "judge")
            ),
            settings.llm_temperature,
            settings.llm_max_tokens,
            settings.llm_reasoning_effort,
            settings.llm_reasoning_effort_manager,
            settings.stream_reasoning,
            _portfolio_key(portfolio),
            # Incognito analyses never merge with normal ones: they ran
            # without decision-memory context and must not be saved or served
            # as if they had.
            run.incognito,
            # P2.9: sizing and gating settings change the result (suggested
            # size, risk flags), so a changed setting must not serve stale
            # cached analyses.
            settings.default_position_size,
            settings.max_position_pct,
            settings.max_invested_pct,
            settings.min_cash_pct,
            settings.max_drawdown_pct,
            settings.max_positions,
            settings.max_correlated_pct,
        )

    def _cache_result(self, key: AnalysisCacheKey, result: StockAnalysis) -> None:
        """Store newest-first and evict the oldest beyond the cap (P2.10)."""
        self.analysis_cache[key] = (time.monotonic(), result)
        self.analysis_cache.move_to_end(key)
        while len(self.analysis_cache) > ANALYSIS_CACHE_MAX_ENTRIES:
            self.analysis_cache.popitem(last=False)

    @staticmethod
    async def _emit_cached(run: Run, result: StockAnalysis, cached: bool = True) -> None:
        await run.emit(
            "ticker_started", {"ticker": result.ticker, "cached": cached}
        )
        await run.emit(
            "ticker_data",
            {
                "ticker": result.ticker,
                "price": result.price,
                "company_name": result.company_name,
                "sources": result.providers,
                "cached": cached,
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
                "cached": cached,
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
                shared = self.analysis_inflight.get(key)
                if shared is task:
                    # The last waiter left: a finished analysis just leaves
                    # the map, an unfinished one (a cancel or the run
                    # deadline) is stopped first, so a later run starts
                    # fresh instead of joining a zombie that hangs until its
                    # own deadline (P2.8). A newer task under this key is
                    # never touched: it belongs to other waiters.
                    self.analysis_inflight.pop(key, None)
                    if not shared.done():
                        shared.cancel()

    async def _analyze_one(
        self,
        run: Run,
        ticker: str,
        portfolio: PortfolioSummary | None,
        held_closes: dict[str, list[float]] | None = None,
    ) -> StockAnalysis:
        # The comparison partner for `What changed` (P1.5), computed once per
        # ticker and applied on every path (fresh, cache hit, inflight join).
        # Incognito skips it: reading past runs would leak saved history into
        # a run that is supposed to know nothing about the user.
        previous = (
            None
            if run.incognito
            else await changes.previous_call(
                run.client_id, ticker, run.run_id, run.started_at
            )
        )
        key = self._cache_key(run, ticker, portfolio)
        cached = self.analysis_cache.get(key)
        if (
            not run.incognito
            and cached
            and time.monotonic() - cached[0] < ANALYSIS_CACHE_TTL_SECONDS
        ):
            self.analysis_cache.move_to_end(key)
            result = cached[1].model_copy(deep=True)
            changes.attach(result, previous)
            logger.info("[analysis] %s cache hit", ticker)
            await self._emit_cached(run, result)
            return result

        task = self.analysis_inflight.get(key)
        if task is not None:
            result = await self._await_inflight(run, key, task)
            if not run.incognito and not result.error:
                self._cache_result(key, result.model_copy(deep=True))
            result = result.model_copy(deep=True)
            changes.attach(result, previous)
            logger.info("[analysis] %s joined cached in-flight analysis", ticker)
            # Incognito never reads the cache, so a shared in-flight analysis
            # must not wear the "cached" badge either.
            await self._emit_cached(run, result, cached=not run.incognito)
            return result

        task = asyncio.create_task(
            analyze_ticker(
                ticker,
                run.emit,
                portfolio_summary=portfolio,
                outlook=run.outlook,
                depth=run.depth,
                run_id=run.run_id,
                held_closes=held_closes,
                previous=previous,
                incognito=run.incognito,
            )
        )
        self.analysis_inflight[key] = task
        result = await self._await_inflight(run, key, task)
        if not run.incognito and not result.error:
            self._cache_result(key, result.model_copy(deep=True))
        return result

    async def _execute(self, run: Run) -> None:
        try:
            # P2.8: every hangable phase runs under the run deadline, so one
            # hung provider call fails this run instead of leaving it
            # "running" forever.
            deadline = settings.run_timeout_minutes * 60
            semaphore = asyncio.Semaphore(settings.max_tickers)
            # One portfolio snapshot per run, shared by every ticker's manager
            # prompt and risk gate (instead of one fetch per ticker), plus one
            # price-history fetch per held ticker for the concentration gate.
            # Incognito never touches the broker: market data only.
            if run.incognito:
                portfolio_summary = None
                held_closes = {}
            else:
                portfolio_summary = await asyncio.wait_for(
                    fetch_portfolio_summary(), deadline
                )
                held_closes = await asyncio.wait_for(
                    fetch_held_closes(portfolio_summary), deadline
                )

            if not run.cancel_requested:
                async def analyze_one(ticker: str) -> StockAnalysis:
                    async with semaphore:
                        try:
                            result = await self._analyze_one(
                                run, ticker, portfolio_summary, held_closes
                            )
                        except Exception as exc:  # noqa: BLE001 - last-resort guard
                            logger.error("[analysis] %s crashed: %s", ticker, redact(str(exc)))
                            result = StockAnalysis(ticker=ticker, error=redact(str(exc))[:300])
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
                results = await asyncio.wait_for(
                    asyncio.gather(*run.ticker_tasks.values()), deadline
                )
                run.results = {result.ticker: result for result in results}
                if results and all(_no_usable_output(r) for r in results):
                    run.status = "failed"
                    run.error = (
                        "every agent failed - check the LLM settings "
                        "(model, base URL, API key)"
                    )
                else:
                    run.status = "completed"
        except asyncio.TimeoutError:
            # The deadline fired under a hung phase: fail the run instead of
            # leaving it "running" forever (P2.8).
            run.status = "failed"
            run.error = f"run timed out after {settings.run_timeout_minutes} min"
            logger.error("[analysis] run %s timed out", run.run_id)
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
            run.error = redact(str(exc))[:300]
            logger.error("[analysis] run %s failed: %s", run.run_id, redact(str(exc)))
        if run.cancel_requested:
            # A cancel accepted at any point wins, even a hair after the
            # gather returned: a cancelled run can never flip to completed.
            run.status = "cancelled"
            run.error = None
        run.completed_at = time.time()
        if not run.incognito:
            await self._persist(run)
            # P2.8: the SQLite row is the run's home once persisted. Finished
            # runs get a short reconnect grace in memory (an SSE tab that
            # dropped mid-run replays its ending), then leave on the next
            # create; get_status, chat, and orders read through run_history.
            self._prune_finished()
        await run.emit(
            "analysis_completed",
            {
                "run_id": run.run_id,
                "duration_s": round(time.time() - run.started_at, 1),
                "status": run.status,
                "error": run.error,
            },
        )

    def _prune_finished(self) -> None:
        """Drop terminal non-incognito runs whose reconnect grace has passed."""
        cutoff = time.time() - FINISHED_RUN_GRACE_SECONDS
        for run_id, run in list(self.runs.items()):
            if (
                not run.incognito
                and run.status != "running"
                and (run.completed_at or 0) < cutoff
            ):
                self.runs.pop(run_id, None)


store = RunStore()
