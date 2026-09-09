"""CLI: uv run python -m app.backtest --tickers NVDA,AMD --start 2024-01-01 --end 2025-06-30

Mock mode is the default (free, deterministic). --llm runs the real agents
after printing a cost estimate and requiring confirmation (or --yes).
Honest-experiment controls (ROADMAP P1.2): fundamentals are excluded from
replays by default (--allow-current-fundamentals opts back in with a loud
warning), --holdout splits tune from untouched test dates, --paired runs a
one-change comparison, and every report writes a reproducibility manifest.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

AGENTS_BY_DEPTH = {"fast": 5, "medium": 7, "expert": 9}
TOKENS_PER_AGENT = 2900  # rough prompt+completion budget per agent call


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.backtest",
        description="Walk-forward backtest of the agent pipeline (ROADMAP 2.2 / P1.2)",
    )
    parser.add_argument("--tickers", required=True, help="comma-separated, e.g. NVDA,AMD")
    parser.add_argument("--start", required=True, help="first decision date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="last decision date, YYYY-MM-DD")
    parser.add_argument("--step", type=int, default=21, help="days between decisions")
    parser.add_argument("--horizon", type=int, default=21, help="days to hold each decision")
    parser.add_argument("--depth", default="fast", choices=["fast", "medium", "expert"])
    parser.add_argument("--outlook", default="short_term",
                        choices=["day_trade", "short_term", "long_term"])
    parser.add_argument("--llm", action="store_true",
                        help="run the real LLM agents (default: free mock mode)")
    parser.add_argument("--short", action="store_true",
                        help="grade SELLs as short returns (default: long-only, SELL=0)")
    parser.add_argument("--out", default=None, help="report directory (default docs/backtests)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the interactive confirmation for --llm cost")
    parser.add_argument("--allow-current-fundamentals", action="store_true",
                        help="replay current-vintage fundamentals (LOOK-AHEAD BIAS: "
                             "leaks later information into historical runs)")
    parser.add_argument("--holdout", default=None, metavar="YYYY-MM-DD",
                        help="dates on/after this form the untouched test period")
    parser.add_argument("--seed", type=int, default=7,
                        help="seed for the deterministic bootstrap intervals")
    parser.add_argument("--paired", default=None, metavar="DIM=A:B",
                        help="run two backtests differing only in DIM "
                             "(depth, rebuttals, forecast, sentiment, model), "
                             "e.g. --paired depth=fast:expert")
    return parser.parse_args(argv)


def confirm_llm_cost(args: argparse.Namespace, runs: int) -> None:
    agents = AGENTS_BY_DEPTH[args.depth] * runs
    tokens = agents * TOKENS_PER_AGENT
    print(
        f"LLM mode: ~{runs} pipeline runs x {AGENTS_BY_DEPTH[args.depth]} agents "
        f"= ~{agents} LLM calls (~{tokens:,} tokens) at your provider's rates.\n"
        "This costs real money and historical dates carry memorization risk."
    )
    if args.yes:
        return
    answer = input("Continue? [yes/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted.")
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)

    from app.backtest.data import FUNDAMENTALS_CURRENT
    from app.backtest.experiments import parse_pair, run_paired
    from app.backtest.run import date_grid, run_backtest

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        raise SystemExit("no valid tickers provided")
    grid = date_grid(args.start, args.end, args.step)
    if not grid:
        raise SystemExit(f"empty date grid for {args.start}..{args.end}")

    mode = "llm" if args.llm else "mock"
    if args.llm:
        confirm_llm_cost(args, len(tickers) * len(grid))

    fundamentals = FUNDAMENTALS_CURRENT if args.allow_current_fundamentals else "excluded"
    common = dict(
        tickers=tickers,
        start=args.start,
        end=args.end,
        step_days=args.step,
        horizon_days=args.horizon,
        depth=args.depth,
        outlook=args.outlook,
        mode=mode,
        short=args.short,
        out_dir=Path(args.out) if args.out else None,
        fundamentals=fundamentals,
        holdout=args.holdout,
        seed=args.seed,
    )

    if args.paired:
        try:
            dimension, left, right = parse_pair(args.paired)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if dimension == "depth":
            # The pair overrides depth; keep --depth out of the shared args.
            common.pop("depth")
        left_result, right_result, report = asyncio.run(run_paired(dimension, left, right, **common))
        for arm, result in (("A", left_result), ("B", right_result)):
            counts = result.overall["counts"]
            print(
                f"arm {arm}: {counts['BUY']} BUY / {counts['SELL']} SELL / "
                f"{counts['HOLD']} HOLD | cumulative "
                f"{result.overall['cumulative_pct']:+.2f}% | "
                f"{result.config['report_md']}"
            )
        print(f"Comparison: {report}")
        return

    result = asyncio.run(run_backtest(**common))
    overall = result.overall
    counts = overall["counts"]
    print(
        f"\n{counts['BUY']} BUY / {counts['SELL']} SELL / {counts['HOLD']} HOLD "
        f"({len(result.ungraded)} ungraded) | hit rate "
        f"{overall['hit_rate_pct'] if overall['hit_rate_pct'] is not None else '-'}% | "
        f"cumulative {overall['cumulative_pct']:+.2f}% | "
        f"Sharpe {overall['sharpe']} | max DD {overall['max_drawdown_pct']:.2f}%"
    )
    print(f"Report: {result.config['report_md']}")
    print(f"Manifest: {result.config['manifest_path']}")
    periods = overall.get("periods")
    if periods:
        print(f"Holdout test period verdict: {periods['test']['verdict']}")


if __name__ == "__main__":
    main()
