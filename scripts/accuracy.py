"""Accuracy report as a script: past calls vs realized performance (P1.9).

Prints every graded unique call with its verdict, per-decision aggregates,
the per-signal comparison, avoided slides, and the pending count. Running it
also back-fills calls whose window closed but were never graded (same lazy
grading the pipeline uses). Read-mostly: the only writes are stored outcomes.

Usage: PYTHONPATH=. uv run python scripts/accuracy.py
"""

import asyncio

from app.accuracy import accuracy_report

VERDICT_MARK = {"right": "+", "wrong": "-", "neutral": ".", "unknown": "?"}


def _pct(value: float | None, width: int = 6) -> str:
    text = "n/a" if value is None else f"{value:+.1f}%"
    return f"{text:>{width}}"


def _money(pct_value: float | None) -> str:
    if pct_value is None:
        return "n/a"
    dollars = (pct_value / 100) * 10000
    return f"-${abs(dollars):,.0f}" if dollars < 0 else f"+${dollars:,.0f}"


def main(report: dict | None = None) -> None:
    # `report` lets a caller inside an event loop (the offline check) render
    # without nesting asyncio.run.
    if report is None:
        report = asyncio.run(accuracy_report())
    horizon = report["horizon_days"]
    print(
        f"{report['graded']} graded unique calls · {report['pending']} pending "
        f"(inside or beyond the {horizon}-day window)"
    )
    print()
    for group in report["by_decision"]:
        if not group["n"]:
            continue
        hit = f"{group['hit_rate']:.0%}" if group["hit_rate"] is not None else "n/a"
        detail = (
            f"{group['right']} slides avoided · {group['wrong']} gains missed"
            if group["decision"] == "HOLD"
            else f"{group['right']} right · {group['wrong']} wrong"
        )
        print(
            f"{group['decision']:<5} n={group['n']:<3} hit {hit:>4} of decisive · "
            f"{detail} · {group['neutral']} neutral · "
            f"mean alpha {_pct(group['mean_alpha_pct'])} · "
            f"mean realized {_pct(group['mean_realized_pct'])}"
        )
    per_signal = report["per_signal"]
    if per_signal["n"]:
        print(
            f"\nper $10,000 signal over {per_signal['n']} windows (long-only): "
            f"following the calls {_money(per_signal['follow_calls_pct'])} · "
            f"always-buy {_money(per_signal['always_buy_pct'])} · "
            f"SPY {_money(per_signal['spy_pct'])}"
        )
    slides = report["avoided_slides"]
    if slides:
        listed = " · ".join(f"{s['ticker']} {s['realized_pct']:.0f}%" for s in slides)
        print(f"HOLDs sidestepped {len(slides)} double-digit slides: {listed}")

    print(
        f"\n{'date':<11}{'tk':<6}{'call':<5}{'conf':>5}{'entry':>9}"
        f"{'realized':>9}{'SPY':>7}{'alpha':>7}  verdict"
    )
    for row in report["rows"]:
        print(
            f"{row['date']:<11}{row['ticker']:<6}{row['decision']:<5}"
            f"{round(row['confidence'] * 100):>4}%"
            f"{row['entry_price']:>9.2f}"
            f"{_pct(row['realized_return_pct'], 9)}"
            f"{_pct(row['spy_return_pct'], 7)}"
            f"{_pct(row['alpha_vs_spy_pct'], 7)}"
            f"  {VERDICT_MARK.get(row['verdict'], '?')} {row['verdict']}"
        )
    print("\nsmall sample: read every rate as an observation, not a statistic")


if __name__ == "__main__":
    main()
