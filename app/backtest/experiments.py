"""Paired backtest experiments: exactly one change at a time (ROADMAP P1.2).

Both arms share the same date grid, the same snapshot cache, the same costs,
and the same seed; only the requested dimension differs. The comparison is
reported on the holdout (test) period when one is set, with a paired
bootstrap interval over the per-(ticker, date) alpha differences. A winner is
promoted only when both arms clear the minimum positioned sample and the
interval excludes zero - otherwise the honest answer is "not distinguishable".

CLI: uv run python -m app.backtest --paired depth=fast:medium ...
Dimensions: depth, rebuttals, forecast, sentiment, model.
"""

import logging
from pathlib import Path

from app.backtest.cache import SnapshotCache
from app.backtest.grade import (
    BOOTSTRAP_REPS,
    MIN_POSITIONED_FOR_PROMOTION,
    BacktestResult,
    aggregate,
    bootstrap_mean_ci,
    promotion_verdict,
)
from app.backtest.report import _fmt, _pct
from app.config import settings

logger = logging.getLogger("backtest")

DIMENSIONS = ("depth", "rebuttals", "forecast", "sentiment", "model")


def parse_pair(spec: str) -> tuple[str, str, str]:
    """`depth=fast:medium` -> ("depth", "fast", "medium")."""
    try:
        dimension, values = spec.split("=", 1)
        left, right = values.split(":", 1)
    except ValueError as exc:
        raise ValueError(
            f"--paired must look like depth=fast:medium, got {spec!r}"
        ) from exc
    if dimension not in DIMENSIONS:
        raise ValueError(
            f"paired dimension must be one of {', '.join(DIMENSIONS)}, got {dimension!r}"
        )
    if not left.strip() or not right.strip():
        raise ValueError(f"--paired needs two values, got {spec!r}")
    if left.strip() == right.strip():
        raise ValueError(f"--paired values must differ, got {spec!r}")
    return dimension, left.strip(), right.strip()


def _arm_overrides(dimension: str, value: str) -> dict:
    """run_backtest kwargs (plus _apply/_model hooks) for one arm of the pair.

    depth changes the agent roster, rebuttals the debate length,
    forecast/sentiment drop one researcher, model swaps the LLM (llm mode
    only). Everything else is shared, which is what makes the pair honest.
    """
    kwargs: dict = {}
    if dimension == "depth":
        kwargs["depth"] = value
    elif dimension == "rebuttals":
        rounds = int(value)
        if rounds < 1 or rounds > 3:
            raise ValueError(f"rebuttals value must be 1-3, got {value!r}")

        def apply(rounds: int = rounds) -> None:
            settings.debate_rounds = rounds

        kwargs["_apply"] = apply
    elif dimension in ("forecast", "sentiment"):
        enabled = int(value)
        if enabled not in (0, 1):
            raise ValueError(f"{dimension} value must be 0 or 1, got {value!r}")
        kwargs["exclude_analysts"] = () if enabled else (dimension,)
    elif dimension == "model":
        kwargs["_model"] = value
    return kwargs


async def run_paired(
    dimension: str,
    left: str,
    right: str,
    **common,
) -> tuple[BacktestResult, BacktestResult, Path]:
    """Run both arms sharing one cache and write the comparison report.

    `common` holds the shared run_backtest arguments (tickers, dates, mode,
    holdout, out_dir, ...). Returns (left result, right result, report path).
    """
    from app.backtest.run import DEFAULT_OUT_DIR, run_backtest

    cache: SnapshotCache = common.pop("cache", None) or SnapshotCache()
    out_dir: Path = common.pop("out_dir", None) or DEFAULT_OUT_DIR
    mode = common.get("mode", "mock")
    original_model = settings.llm_model
    original_rounds = settings.debate_rounds
    results = []
    try:
        for label, value in (("left", left), ("right", right)):
            kwargs = _arm_overrides(dimension, value)
            apply_fn = kwargs.pop("_apply", None)
            model = kwargs.pop("_model", None)
            if model is not None:
                if mode != "llm":
                    raise ValueError("model pairs need --llm mode")
                settings.llm_model = model
            if apply_fn is not None:
                apply_fn()
            results.append(
                await run_backtest(
                    **common,
                    cache=cache,
                    out_dir=out_dir,
                    name=f"{mode}-{dimension}-{label}",
                    **kwargs,
                )
            )
            settings.llm_model = original_model
            settings.debate_rounds = original_rounds
    finally:
        settings.llm_model = original_model
        settings.debate_rounds = original_rounds

    left_result, right_result = results
    report_path = out_dir / f"report-paired-{dimension}.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _comparison_md(dimension, left, right, left_result, right_result),
        encoding="utf-8",
    )
    return left_result, right_result, report_path


def _paired_alphas(
    left: BacktestResult, right: BacktestResult, holdout: str | None
) -> tuple[list[float], dict[tuple[str, str], float], dict[tuple[str, str], float]]:
    """Alpha differences over (ticker, date) pairs both arms positioned.

    HOLD decisions and unmatched dates carry no alpha, so they drop out of a
    paired comparison rather than fabricating a zero difference.
    """
    def collect(result: BacktestResult) -> dict[tuple[str, str], float]:
        return {
            (o.ticker, o.date): o.alpha_pct
            for o in result.outcomes
            if o.alpha_pct is not None and _in_holdout(o, holdout)
        }

    left_map, right_map = collect(left), collect(right)
    shared = sorted(set(left_map) & set(right_map))
    differences = [left_map[key] - right_map[key] for key in shared]
    return differences, left_map, right_map


def _in_holdout(outcome, holdout: str | None) -> bool:
    return holdout is None or outcome.date >= holdout


def _arm_summary(result: BacktestResult, holdout: str | None) -> dict:
    outcomes = [o for o in result.outcomes if _in_holdout(o, holdout)]
    alphas = [o.alpha_pct for o in outcomes if o.alpha_pct is not None]
    stats = aggregate(outcomes, result.config["step_days"])
    stats["positioned_n"] = len(alphas)
    stats["alpha_ci_pct"] = bootstrap_mean_ci(
        alphas, seed=result.config.get("seed", 7)
    )
    return stats


def _comparison_md(
    dimension: str,
    left: str,
    right: str,
    left_result: BacktestResult,
    right_result: BacktestResult,
) -> str:
    holdout = left_result.config.get("holdout")
    scope = (
        f"test period (dates >= {holdout})"
        if holdout
        else "whole grid (no holdout set - set --holdout before promoting anything)"
    )
    left_stats = _arm_summary(left_result, holdout)
    right_stats = _arm_summary(right_result, holdout)
    differences, left_map, right_map = _paired_alphas(
        left_result, right_result, holdout
    )
    ci = bootstrap_mean_ci(differences, seed=left_result.config.get("seed", 7))
    paired_n = len(differences)
    if paired_n < MIN_POSITIONED_FOR_PROMOTION:
        verdict = (
            f"insufficient paired sample (n={paired_n} < "
            f"{MIN_POSITIONED_FOR_PROMOTION}): do not promote either arm"
        )
    elif ci is None:
        verdict = "uncertainty could not be estimated"
    elif ci[0] <= 0 <= ci[1]:
        verdict = (
            f"difference CI [{_fmt(ci[0])}%, {_fmt(ci[1])}%] crosses 0: the arms "
            "are not distinguishable at this sample size"
        )
    else:
        winner = left if ci[0] > 0 else right
        verdict = (
            f"difference CI [{_fmt(ci[0])}%, {_fmt(ci[1])}%] excludes 0 with "
            f"n={paired_n}: {dimension}={winner} is the better arm on this scope "
            "only - one scope, one change, no stacking of tuned winners"
        )

    def arm_row(label: str, value: str, stats: dict) -> str:
        return (
            f"| {label} ({dimension}={value}) | {stats['decisions']} | "
            f"{stats['positioned_n']} | {_pct(stats['avg_alpha_pct'])} | "
            f"{_pct(stats['cumulative_pct'])} |"
        )

    lines = [
        f"# Paired experiment - {dimension}",
        "",
        f"Generated from reports `{left_result.config['report_md']}` and "
        f"`{right_result.config['report_md']}`; same cache, same dates, same "
        f"costs, same seed. Only `{dimension}` differs.",
        "",
        f"Scope: **{scope}**. Sample sizes and intervals below; do not quote "
        "either arm without them.",
        "",
        "| Arm | Decisions | Positioned (alpha) | Mean alpha | Cumulative |",
        "|---|---|---|---|---|",
        arm_row("A", left, left_stats),
        arm_row("B", right, right_stats),
        "",
        "## Paired difference (A - B, matched by ticker and date)",
        "",
        f"- paired positioned decisions: {paired_n} "
        f"(A alone positioned {len(left_map)}, B alone {len(right_map)})",
        f"- mean difference: {_pct(sum(differences) / paired_n) if paired_n else '-'}",
        f"- 95% bootstrap CI over {BOOTSTRAP_REPS} resamples: "
        + ("-" if ci is None else f"[{_fmt(ci[0])}%, {_fmt(ci[1])}%]"),
        f"- verdict: {verdict}.",
        "",
        f"- arm A verdict on its own: {promotion_verdict(left_stats['positioned_n'], left_stats['alpha_ci_pct'])}.",
        f"- arm B verdict on its own: {promotion_verdict(right_stats['positioned_n'], right_stats['alpha_ci_pct'])}.",
        "",
        "---",
        "Educational project - a simulation comparison, not investment advice.",
        "",
    ]
    return "\n".join(lines)
