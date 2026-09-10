"""Walk-forward orchestration: snapshots -> agent pipeline -> grading -> report.

Mock mode is the free default; `mode="llm"` runs the real agents (cost gated
in the CLI). Decisions near the end of the grid whose horizon window has not
finished yet are reported as ungraded, never dropped silently.

Honest-experiment defaults (ROADMAP P1.2): fundamentals are excluded from
replays (no point-in-time source; opt in with fundamentals="current" and a
loud report warning), every report carries a reproducibility manifest, cheap
baselines (all-HOLD, buy-and-hold, deterministic momentum) sit beside the
pipeline's numbers, an optional holdout date splits tune from untouched test
periods, and every period reports its sample size, a bootstrap CI for mean
alpha, and a plain promotion verdict.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.backtest.cache import SnapshotCache, offline_mode
from app.backtest.data import (
    FUNDAMENTALS_CURRENT,
    FUNDAMENTALS_EXCLUDED,
    LOOKBACK_DAYS,
    NEWS_DAYS,
    build_snapshot,
)
from app.backtest.grade import (
    BacktestResult,
    Decision,
    aggregate,
    buy_hold_pct,
    grade,
    momentum_decision,
    period_stats,
)
from app.backtest.manifest import build_manifest, write_manifest
from app.backtest.report import build_flags, write_report
from app.config import settings
from app.depth import depth_profile
from app.tools.market_data import get_closes_between

logger = logging.getLogger("backtest")

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent.parent / "backtests"


def date_grid(start: str, end: str, step_days: int) -> list[str]:
    """Inclusive walk-forward grid of decision dates."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise ValueError(f"end {end} before start {start}")
    step = timedelta(days=max(1, step_days))
    grid, cursor = [], first
    while cursor <= last:
        grid.append(cursor.isoformat())
        cursor += step
    return grid


async def _noop_emit(kind: str, payload: dict) -> None:  # noqa: ARG001
    """Backtests consume results, not the SSE stream."""


async def _series(
    ticker: str, start: str, end: str, cache: SnapshotCache, offline: bool
) -> dict[str, float]:
    """Close series for grading, cached so warm re-runs stay offline."""
    cached = cache.get_series(ticker, start, end)
    if cached is not None:
        return cached
    if offline:
        raise RuntimeError(f"offline mode: no cached price series for {ticker}")
    closes = await get_closes_between(ticker, start, end)
    if closes:
        cache.put_series(ticker, start, end, closes)
    return closes


async def run_backtest(
    tickers: list[str],
    start: str,
    end: str,
    step_days: int = 21,
    horizon_days: int = 21,
    depth: str = "fast",
    outlook: str = "short_term",
    mode: str = "mock",
    short: bool = False,
    out_dir: Path | None = None,
    cache: SnapshotCache | None = None,
    fundamentals: str = FUNDAMENTALS_EXCLUDED,
    holdout: str | None = None,
    seed: int = 7,
    name: str | None = None,
    exclude_analysts: tuple[str, ...] | list[str] = (),
) -> BacktestResult:
    """Run the pipeline at every grid date and grade the decisions.

    Mock mode (default) forces the rule-based agents even when an LLM key is
    configured; TimeGPT is disabled either way unless the caller re-enables
    it, so backtests stay free and deterministic.

    `fundamentals="excluded"` (default) omits fundamentals from snapshots and
    drops the fundamental analyst from the replay - there is no point-in-time
    source, so current-vintage values would leak later information.
    `fundamentals="current"` opts back in and the report says so loudly.
    `holdout` splits the grid into a tune period (< holdout) and an untouched
    test period (>= holdout); headline numbers belong to the test period.
    `exclude_analysts` drops further researchers (paired experiments).
    """
    from app import workflow
    from app.calibration import DECISION_POLICY_VERSION

    if mode not in ("mock", "llm"):
        raise ValueError(f"mode must be mock or llm, got {mode!r}")
    if fundamentals not in (FUNDAMENTALS_EXCLUDED, FUNDAMENTALS_CURRENT):
        raise ValueError(f"fundamentals must be excluded or current, got {fundamentals!r}")
    if holdout is not None:
        try:
            date.fromisoformat(holdout)
        except ValueError as exc:
            raise ValueError(f"holdout must be YYYY-MM-DD, got {holdout!r}") from exc
    offline = offline_mode()
    cache = cache or SnapshotCache()
    out_dir = out_dir or DEFAULT_OUT_DIR
    name = name or mode

    if mode == "mock":
        settings.llm_api_key = ""
        workflow._llms.clear()
        workflow._llm_roles_initialized.clear()
    settings.nixtla_api_key = ""  # no paid forecasts inside a backtest

    profile = depth_profile(depth)
    exclusions = tuple(exclude_analysts)
    if fundamentals == FUNDAMENTALS_EXCLUDED and "fundamental" in profile["research"]:
        exclusions += ("fundamental",)

    grid = date_grid(start, end, step_days)
    jobs = [(ticker, day) for ticker in tickers for day in grid]
    logger.info(
        "[backtest] %s mode, %d tickers x %d dates = %d runs",
        mode,
        len(tickers),
        len(grid),
        len(jobs),
    )

    # 1. Point-in-time snapshots (bounded concurrency, cached).
    snapshot_sem = asyncio.Semaphore(4)
    snapshots: dict[tuple[str, str], object] = {}

    async def snap(ticker: str, day: str) -> None:
        async with snapshot_sem:
            try:
                snapshots[(ticker, day)] = await build_snapshot(
                    ticker, day, cache, offline=offline, fundamentals=fundamentals
                )
            except Exception as exc:  # noqa: BLE001 - one bad date skips one run
                logger.warning("[backtest] %s @ %s: snapshot failed: %s", ticker, day, exc)
                snapshots[(ticker, day)] = None

    await asyncio.gather(*(snap(ticker, day) for ticker, day in jobs))

    # 2. Close series for grading (lookback to the first grid date so entries
    #    always resolve, through tomorrow so exits do too).
    grade_start = (
        date.fromisoformat(start) - timedelta(days=LOOKBACK_DAYS)
    ).isoformat()
    grade_end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    series, spy_series = await asyncio.gather(
        *(
            asyncio.gather(*(_series(t, grade_start, grade_end, cache, offline) for t in tickers)),
            _series("SPY", grade_start, grade_end, cache, offline),
        )
    )
    closes_by_ticker = dict(zip(tickers, series))

    # 3. Run the pipeline at each date; replay mode passes the snapshot and
    #    disables all live context (portfolio, memory) - no look-ahead.
    run_sem = asyncio.Semaphore(4)

    async def analyze(ticker: str, day: str):
        if snapshots.get((ticker, day)) is None:
            return None  # snapshot failed; never fall back to live data
        async with run_sem:
            try:
                return await workflow.analyze_ticker(
                    ticker,
                    _noop_emit,
                    outlook=outlook,
                    depth=depth,
                    market_data=snapshots[(ticker, day)],
                    live_context=False,
                    exclude_analysts=exclusions,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[backtest] %s @ %s: analysis failed: %s", ticker, day, exc)
                return None

    analyses = await asyncio.gather(*(analyze(t, d) for t, d in jobs))

    # 4. Grade, plus the cheap baselines the pipeline must beat (P1.2).
    outcomes, ungraded = [], []
    momentum_outcomes = []
    for (ticker, day), analysis in zip(jobs, analyses):
        if analysis is None or analysis.error:
            continue
        decision = Decision(ticker, day, analysis.decision, analysis.confidence)
        outcome = grade(
            decision,
            closes_by_ticker.get(ticker, {}),
            spy_series,
            horizon_days,
            short=short,
        )
        (outcomes if outcome is not None else ungraded).append(outcome or decision)
    for (ticker, day), snapshot in snapshots.items():
        if snapshot is None:
            continue
        baseline = Decision(
            ticker, day, momentum_decision(snapshot.closes, outlook), 0.5
        )
        momentum_outcome = grade(
            baseline, closes_by_ticker.get(ticker, {}), spy_series, horizon_days, short=short
        )
        if momentum_outcome is not None:
            momentum_outcomes.append(momentum_outcome)
    outcomes.sort(key=lambda o: (o.date, o.ticker))
    momentum_outcomes.sort(key=lambda o: (o.date, o.ticker))

    # 5. Aggregate per ticker + overall, with baselines and period splits.
    per_ticker: dict[str, dict] = {}
    for ticker in tickers:
        own = [o for o in outcomes if o.ticker == ticker]
        metrics = aggregate(own, step_days)
        metrics["buy_hold_pct"] = buy_hold_pct(closes_by_ticker.get(ticker, {}), own)
        per_ticker[ticker] = metrics
    overall = aggregate(outcomes, step_days)
    overall["spy_cumulative_pct"] = buy_hold_pct(spy_series, outcomes)
    overall["baselines"] = {
        "all_hold": {
            "decisions": len(outcomes),
            "cumulative_pct": 0.0,
            "note": "every decision HOLD scores 0 net after costs",
        },
        "momentum": period_stats(momentum_outcomes, step_days, seed=seed),
    }
    overall["periods"] = None
    if holdout is not None:
        overall["periods"] = {
            "tune": period_stats(
                [o for o in outcomes if o.date < holdout], step_days, seed=seed
            ),
            "test": period_stats(
                [o for o in outcomes if o.date >= holdout], step_days, seed=seed
            ),
        }

    config = {
        "mode": mode,
        "name": name,
        "model": settings.llm_model if mode == "llm" else "rule-based mock",
        "tickers": tickers,
        "start": start,
        "end": end,
        "step_days": step_days,
        "horizon_days": horizon_days,
        "depth": depth,
        "outlook": outlook,
        "cost_pct": 0.10,
        "short": short,
        "fundamentals": fundamentals,
        "holdout": holdout,
        "seed": seed,
        "excluded_analysts": list(exclusions),
        "policy_version": DECISION_POLICY_VERSION,
        "debate_rounds": settings.debate_rounds,
    }
    result = BacktestResult(
        config=config,
        flags=build_flags(mode, fundamentals),
        tickers=per_ticker,
        overall=overall,
        outcomes=outcomes,
        ungraded=ungraded,
    )
    snapshot_payloads = {
        (ticker, day): cache.get_snapshot(ticker, day, LOOKBACK_DAYS, NEWS_DAYS)
        for (ticker, day) in jobs
    }
    manifest = build_manifest(config, snapshot_payloads)
    json_path, md_path = write_report(result, out_dir, name)
    manifest_path = write_manifest(manifest, out_dir, name)
    logger.info("[backtest] report written: %s", md_path)
    result.config["report_json"] = str(json_path)
    result.config["report_md"] = str(md_path)
    result.config["manifest_path"] = str(manifest_path)
    return result
