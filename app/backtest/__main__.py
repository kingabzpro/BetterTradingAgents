"""CLI: uv run python -m app.backtest --tickers NVDA,AMD --start 2024-01-01 --end 2025-06-30

Mock mode is the default (free, deterministic). --llm runs the real agents
after printing a cost estimate and requiring confirmation (or --yes).
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
        description="Walk-forward backtest of the agent pipeline (ROADMAP 2.2)",
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

    from app.backtest.run import date_grid, run_backtest

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        raise SystemExit("no valid tickers provided")
    grid = date_grid(args.start, args.end, args.step)
    if not grid:
        raise SystemExit(f"empty date grid for {args.start}..{args.end}")

    if args.llm:
        confirm_llm_cost(args, len(tickers) * len(grid))

    result = asyncio.run(
        run_backtest(
            tickers=tickers,
            start=args.start,
            end=args.end,
            step_days=args.step,
            horizon_days=args.horizon,
            depth=args.depth,
            outlook=args.outlook,
            mode="llm" if args.llm else "mock",
            short=args.short,
            out_dir=Path(args.out) if args.out else None,
        )
    )
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


if __name__ == "__main__":
    main()
