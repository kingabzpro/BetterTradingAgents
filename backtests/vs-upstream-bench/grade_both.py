"""Grade every framework's decisions through BTA's own grading rule.

Run from the BTA repo: uv run python C:/Windows/Temp/ta-bench/grade_both.py
Read-only: compute_outcome/verdict are pure; nothing touches portfolio.db.
BTA decisions are parsed from the generated backtest reports (fast/pro/max);
upstream's 5-way rating maps Overweight->BUY, Underweight->SELL. Long-only
accounting: money only on BUY, SELL/HOLD sit in cash, like the harness.
"""

import asyncio
import json
import re
import sys

sys.path.insert(0, r"C:\Repository\GitHub\BetterTradingAgents")

from app.memory import _fetch_closes_pair, compute_outcome, verdict

RATE_IN, RATE_OUT = 0.30, 1.20  # DeepSeek Flash list, USD per 1M tokens
POSITION_USD = 10_000
REPO = r"C:\Repository\GitHub\BetterTradingAgents"

RATING_MAP = {
    "buy": "BUY", "overweight": "BUY",
    "hold": "HOLD",
    "underweight": "SELL", "sell": "SELL",
}

REPORTS = [
    ("BTA fast", rf"{REPO}\backtests\report-vs-upstream-llm.md"),
    ("BTA pro", rf"{REPO}\backtests\report-vs-upstream-pro.md"),
    ("BTA max", rf"{REPO}\backtests\report-vs-upstream-max.md"),
]


def parse_report_decisions(path: str) -> list[tuple]:
    """(ticker, date, decision, confidence) rows from a report's Decisions table."""
    rows = []
    in_decisions = False
    for line in open(path, encoding="utf-8"):
        if line.startswith("## Decisions"):
            in_decisions = True
            continue
        if in_decisions:
            if line.startswith("|"):
                parts = [p.strip() for p in line.strip().strip("|").split("|")]
                if len(parts) >= 4 and re.fullmatch(r"[A-Z.]+", parts[0]) \
                        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
                    conf = float(parts[3]) if parts[3].replace(".", "").isdigit() else 0.0
                    rows.append((parts[0], parts[1], parts[2], conf))
            elif rows:
                break
    return rows


def load_upstream() -> list[tuple]:
    data = json.load(open(r"C:/Windows/Temp/ta-bench/upstream_runs.json", encoding="utf-8"))
    rows = []
    for c in data["cells"]:
        if not c.get("ok"):
            rows.append((c["ticker"], c["date"], f"FAILED: {c.get('error', '')[:60]}", None,
                         c["seconds"], c["calls"], c["input_tokens"] + c["output_tokens"]))
            continue
        dec = RATING_MAP.get(str(c["decision"]).strip().lower(), "HOLD")
        rows.append((c["ticker"], c["date"], dec, None, c["seconds"], c["calls"],
                     c["input_tokens"] + c["output_tokens"]))
    return rows


async def main() -> None:
    frameworks = [(name, parse_report_decisions(path)) for name, path in REPORTS]
    frameworks.append(("upstream", load_upstream()))

    needed = sorted({(r[0], r[1]) for _, rows in frameworks for r in rows
                     if not str(r[2]).startswith("FAILED")})
    start = min(d for _, d in needed)
    _, spy = await _fetch_closes_pair("SPY", start)
    closes: dict[str, dict[str, float]] = {}
    for ticker in sorted({t for t, _ in needed}):
        own, _ = await _fetch_closes_pair(ticker, start)
        closes[ticker] = own

    def grade(ticker: str, day: str, decision: str):
        row = {"ticker": ticker, "date": day, "decision": decision,
               "confidence": 0.0, "price_at_decision": 0.0}
        outcome = compute_outcome(row, closes.get(ticker, {}), spy)
        if outcome is None:
            return None
        v = verdict(decision, outcome["realized_return_pct"], outcome["alpha_vs_spy_pct"])
        return v, outcome["realized_return_pct"], outcome["alpha_vs_spy_pct"]

    print(f"{'framework':10s} {'ticker':5s} {'date':10s} {'decision':9s} "
          f"{'conf':>4s} {'realized':>9s} {'alpha':>8s} {'pnl$':>7s}  verdict")
    for name, rows in frameworks:
        entries = []
        for row in rows:
            ticker, day, dec = row[0], row[1], row[2]
            conf = f"{row[3]:.2f}" if len(row) > 3 and row[3] is not None else "-"
            if str(dec).startswith("FAILED"):
                print(f"{name:10s} {ticker:5s} {day} {dec}")
                continue
            g = grade(ticker, day, dec)
            if g is None:
                print(f"{name:10s} {ticker:5s} {day} {dec:9s}  ungradeable")
                continue
            v, realized, alpha = g
            entries.append((dec, v, realized, alpha))
            pnl = POSITION_USD * realized / 100 if dec == "BUY" else 0.0
            alpha_s = "n/a" if alpha is None else f"{alpha:+7.2f}%"
            print(f"{name:10s} {ticker:5s} {day} {dec:9s} {conf:>4s} {realized:+8.2f}% "
                  f"{alpha_s} {pnl:+7.0f}  {v}")

        n = len(entries)
        right = sum(1 for _, v, _, _ in entries if v == "right")
        wrong = sum(1 for _, v, _, _ in entries if v == "wrong")
        alphas = [a for _, _, _, a in entries if a is not None]
        positioned = [POSITION_USD * r / 100 for d, _, r, _ in entries if d == "BUY"]
        gains = sum(p for p in positioned if p > 0)
        losses = sum(p for p in positioned if p <= 0)
        pnl = sum(positioned)
        mean_alpha = sum(alphas) / len(alphas) if alphas else 0.0
        print(f"\n{name}: n={n} right={right} wrong={wrong} hit={right}/{right + wrong} "
              f"mean_alpha={mean_alpha:+.2f}% | net={pnl:+,.0f} USD "
              f"(gains {gains:+,.0f} / losses {losses:+,.0f}) | "
              f"avg per decision {pnl / n:+,.0f} USD ({pnl / n / POSITION_USD * 100:+.2f}%)\n")


if __name__ == "__main__":
    asyncio.run(main())
