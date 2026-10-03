"""Per-agent attribution: which researcher's signals matched reality.

Read-only. Joins graded decisions (the accuracy page's own rows) to the saved
run results, and scores each analyst's directional signal against the stock's
realized return over the decision window. Neutral signals and unknown outcomes
are skipped, never guessed. Sample sizes stay printed: with a handful of
graded calls this is an observation tool, not a verdict.

Usage: PYTHONPATH=. uv run python scripts/attribution.py
"""

import json
import sqlite3

from app.config import settings
from app.memory import verdict

AGENTS = ("market", "technical", "fundamental", "news", "sentiment", "forecast")
BULLISH = ("bullish", "positive")
BEARISH = ("bearish", "negative")


def load_calls() -> list[dict]:
    """Unique graded decisions joined to their run's saved agent signals."""
    con = sqlite3.connect(settings.db_path)
    con.row_factory = sqlite3.Row
    decisions = con.execute(
        "SELECT * FROM decisions WHERE mature = 1 AND realized_return_pct IS NOT NULL "
        "ORDER BY date DESC, id DESC"
    ).fetchall()
    results: dict[str, dict] = {}
    for run in con.execute("SELECT run_id, results_json FROM analysis_runs"):
        try:
            results[run["run_id"]] = json.loads(run["results_json"] or "{}")
        except json.JSONDecodeError:
            continue
    con.close()

    calls: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in decisions:
        key = (row["ticker"], row["date"], row["decision"])
        if key in seen:
            continue
        seen.add(key)
        analysis = results.get(row["run_id"], {}).get(row["ticker"], {})
        signals = {
            agent: (analysis.get(agent) or {}).get("signal")
            for agent in AGENTS
            if isinstance(analysis.get(agent), dict)
        }
        calls.append({**dict(row), "signals": signals})
    return calls


def direction(signal: str | None) -> int:
    if signal in BULLISH:
        return 1
    if signal in BEARISH:
        return -1
    return 0


def main() -> None:
    calls = load_calls()
    print(f"{len(calls)} unique graded calls\n")

    # ---- per-call matrix -----------------------------------------------------
    header = f"{'date':<11}{'tk':<6}{'call':<5}{'realized':>9}  " + " ".join(f"{a[:3]:>4}" for a in AGENTS)
    print(header)
    for call in calls:
        marks = []
        realized = call["realized_return_pct"] or 0.0
        outcome_sign = 1 if realized > 0 else (-1 if realized < 0 else 0)
        for agent in AGENTS:
            d = direction(call["signals"].get(agent))
            if d == 0 or outcome_sign == 0:
                marks.append("  . " if d == 0 else "  0 ")
            else:
                marks.append("  + " if d == outcome_sign else "  - ")
        print(
            f"{call['date']:<11}{call['ticker']:<6}{call['decision']:<5}"
            f"{realized:>+8.1f}%  " + " ".join(marks)
        )
    print("(+ signal matched the realized move, - opposed it, . neutral/absent, 0 stock flat)\n")

    # ---- per-agent totals ----------------------------------------------------
    print(f"{'agent':<12}{'calls':>6}{'bull':>6}{'bear':>6}{'neutral':>9}{'scored':>8}{'right':>7}{'hit rate':>10}")
    for agent in AGENTS:
        calls_with = [c for c in calls if agent in c["signals"]]
        bull = sum(1 for c in calls_with if direction(c["signals"][agent]) > 0)
        bear = sum(1 for c in calls_with if direction(c["signals"][agent]) < 0)
        neutral = len(calls_with) - bull - bear
        scored, right = 0, 0
        for call in calls_with:
            d = direction(call["signals"][agent])
            realized = call["realized_return_pct"] or 0.0
            if d == 0 or realized == 0:
                continue
            scored += 1
            if d == (1 if realized > 0 else -1):
                right += 1
        hit = f"{right / scored:.0%}" if scored else "n/a"
        print(f"{agent:<12}{len(calls_with):>6}{bull:>6}{bear:>6}{neutral:>9}{scored:>8}{right:>7}{hit:>10}")

    # ---- what the calls themselves did ---------------------------------------
    counts: dict[str, int] = {}
    for call in calls:
        v = verdict(call["decision"], call["realized_return_pct"] or 0.0, call["alpha_vs_spy_pct"])
        counts[v] = counts.get(v, 0) + 1
    print(f"\ncall verdicts: {counts}")
    print("small sample: read every rate above as an observation, not a statistic")


if __name__ == "__main__":
    main()
