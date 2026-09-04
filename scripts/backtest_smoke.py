"""Weekly backtest smoke (ROADMAP 2.2): 3 tickers x 6 dates, mock mode.

Run: PYTHONPATH=. uv run python scripts/backtest_smoke.py
Regenerates docs/backtests/report-mock.{md,json}; with a warm cache it makes
zero network calls (the first run fetches ~40 provider requests once).
"""

import asyncio
import logging
from datetime import date, timedelta
from pathlib import Path

from app.backtest.run import run_backtest

TICKERS = ["NVDA", "AMD", "META"]
DATES = 6
STEP_DAYS = 21
HORIZON_DAYS = 21

logging.basicConfig(level=logging.INFO, format="%(message)s")

today = date.today()
# Every decision's horizon window must have finished: the last grid date is
# start + (DATES-1)*step and its exit lands start + DATES*step + horizon-ish.
start = today - timedelta(days=DATES * STEP_DAYS + HORIZON_DAYS + 30)
end = start + timedelta(days=(DATES - 1) * STEP_DAYS)

result = asyncio.run(
    run_backtest(
        tickers=TICKERS,
        start=start.isoformat(),
        end=end.isoformat(),
        step_days=STEP_DAYS,
        horizon_days=HORIZON_DAYS,
        depth="fast",
        mode="mock",
        out_dir=Path(__file__).resolve().parent.parent / "docs" / "backtests",
    )
)
counts = result.overall["counts"]
print(
    f"\nsmoke: {counts['BUY']} BUY / {counts['SELL']} SELL / {counts['HOLD']} HOLD, "
    f"cumulative {result.overall['cumulative_pct']:+.2f}%, "
    f"report: {result.config['report_md']}"
)
