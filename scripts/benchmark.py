"""Benchmark scorecard: grade every past decision against realized prices.

Read-only. Sweeps the decisions table plus any run that predates decision
recording (recovered from analysis_runs.results_json), dedupes re-run
duplicates, grades each unique call with the app's own rules (app/memory.py),
and prints the scorecard. `--markdown` emits a README-ready block.

Usage: uv run python scripts/benchmark.py [--markdown]
"""

import asyncio
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.memory import compute_outcome, verdict

POSITION_USD = settings.default_position_size
CRASH_PCT = -10.0  # a matured window this far down is a "collapse"


def load_decisions() -> list[dict]:
    """One row per unique call: the decisions table plus recovered runs."""
    con = sqlite3.connect(settings.db_path)
    con.row_factory = sqlite3.Row
    rows: list[dict] = []
    seen: set = set()

    def add(ticker, day, decision, confidence, price):
        key = (ticker, day, decision)
        if key not in seen:
            seen.add(key)
            rows.append(
                {
                    "ticker": ticker,
                    "date": day,
                    "decision": decision,
                    "confidence": confidence or 0.0,
                    "price_at_decision": price or 0.0,
                }
            )

    for r in con.execute("select * from decisions order by id"):
        # re-runs and autopilot sessions duplicate the same call
        add(r["ticker"], r["date"], r["decision"], r["confidence"], r["price_at_decision"])
    for run in con.execute("select started_at, results_json from analysis_runs"):
        results = json.loads(run["results_json"] or "{}")
        for ticker, a in results.items():
            if not isinstance(a, dict) or not a.get("decision"):
                continue
            day = a.get("as_of", "")[:10]
            if not day:
                day = datetime.fromtimestamp(run["started_at"]).date().isoformat()
            add(ticker, day, a["decision"], a.get("confidence"), a.get("price"))
    con.close()
    return rows


def names(pairs) -> str:
    return ", ".join(f"{r['ticker']} {o['realized_return_pct']:+.0f}%" for r, o in pairs)


def money(x: float) -> str:
    return f"-${abs(x):,.0f}" if x < 0 else f"+${x:,.0f}"


async def main(markdown: bool) -> None:
    from app.memory import _fetch_closes_pair

    decisions = load_decisions()
    per_ticker_cost: list[float] = []
    per_run_cost: list[float] = []
    with sqlite3.connect(settings.db_path) as con:
        runs = con.execute("select count(*) from analysis_runs").fetchone()[0]
        for (blob,) in con.execute("select results_json from analysis_runs"):
            total, priced = 0.0, False
            for a in json.loads(blob or "{}").values():
                usd = isinstance(a, dict) and (a.get("cost_estimate") or {}).get("total_usd")
                if usd:
                    per_ticker_cost.append(usd)
                    total += usd
                    priced = True
            if priced:
                per_run_cost.append(total)

    closes: dict[str, dict[str, float]] = {}
    _, spy_closes = await _fetch_closes_pair(
        "SPY", min(d["date"] for d in decisions)
    )
    for ticker in sorted({d["ticker"] for d in decisions}):
        start = min(d["date"] for d in decisions if d["ticker"] == ticker)
        own, _ = await _fetch_closes_pair(ticker, start)
        closes[ticker] = own

    graded = []
    for row in decisions:
        outcome = compute_outcome(row, closes.get(row["ticker"], {}), spy_closes)
        if outcome is None:
            continue
        v = verdict(
            row["decision"], outcome["realized_return_pct"], outcome["alpha_vs_spy_pct"]
        )
        graded.append((row, outcome, v))

    matured = [(r, o, v) for r, o, v in graded if o["mature"]]
    pending = len(graded) - len(matured)
    today = datetime.now(timezone.utc).date().isoformat()

    if not matured:
        line = "No decisions have matured yet; the track record starts 21 days after the first run."
        print(line)
        if markdown:
            print(f"\n> {line}")
        return

    by_type: dict = defaultdict(lambda: defaultdict(int))
    for r, o, v in matured:
        by_type[r["decision"]][v] += 1
    crashes = [(r, o) for r, o, v in matured if o["realized_return_pct"] <= CRASH_PCT]
    crashes_avoided = [(r, o) for r, o in crashes if r["decision"] == "HOLD"]
    rallies = [(r, o) for r, o, v in matured if o["realized_return_pct"] >= -CRASH_PCT]
    rallies_missed = [(r, o) for r, o in rallies if r["decision"] == "HOLD"]

    agent_usd = sum(
        POSITION_USD * o["realized_return_pct"] / 100
        for r, o, v in matured
        if r["decision"] == "BUY"
    )
    always_usd = sum(POSITION_USD * o["realized_return_pct"] / 100 for _, o, _ in matured)
    spy_mean = [
        o["spy_return_pct"] for _, o, _ in matured if o["spy_return_pct"] is not None
    ]
    spy_usd = sum(POSITION_USD * s / 100 for s in spy_mean)

    if not markdown:
        print(f"runs={runs} unique_calls={len(decisions)} graded={len(graded)} "
              f"matured={len(matured)} pending={pending} as_of={today}")
        for decision, counts in sorted(by_type.items()):
            n = sum(counts.values())
            decisive = counts["right"] + counts["wrong"]
            acc = counts["right"] / decisive * 100 if decisive else 0
            print(f"{decision:4s} matured={n:3d} right={counts['right']} "
                  f"wrong={counts['wrong']} neutral={counts['neutral']} accuracy={acc:.0f}%")
        for r, o, v in sorted(matured, key=lambda x: (x[0]["date"], x[0]["ticker"])):
            alpha = o["alpha_vs_spy_pct"]
            alpha = "      n/a" if alpha is None else f"{alpha:+7.2f}%"
            print(f"  {r['date']} {r['ticker']:5s} {r['decision']:4s} conf={r['confidence']:.2f} "
                  f"realized={o['realized_return_pct']:+7.2f}% alpha={alpha} -> {v}")
        print(f"crashes (<={CRASH_PCT:.0f}%): {len(crashes)} seen, {len(crashes_avoided)} sidestepped")
        print(f"rallies (>={-CRASH_PCT:.0f}%): {len(rallies)} seen, {len(rallies_missed)} missed by HOLDs")
        print(f"pnl at ${POSITION_USD:.0f}/signal: agent={money(agent_usd)} "
              f"always-buy={money(always_usd)} spy={money(spy_usd)}")
        if per_ticker_cost:
            print(f"cost per analysis: ${sum(per_ticker_cost) / len(per_ticker_cost):.3f}/ticker, "
                  f"${sum(per_run_cost) / len(per_run_cost):.3f}/run "
                  f"across {len(per_run_cost)} cost-tracked runs")
        return

    holds = by_type["HOLD"]
    print(f"""## Live track record

Every completed call is graded 21 days later against real closes: its own
return and alpha vs SPY. This scorecard is generated, not hand-written:
`uv run python scripts/benchmark.py --markdown` rebuilds it from the local
run history.

As of {today}: {runs} live sessions, {len(decisions)} unique calls,
{len(matured)} fully graded ({pending} still inside their 21-day window).

| What the market did next (21-day window) | The agent's record |
|---|---|
| {len(crashes)} candidates fell more than {-CRASH_PCT:.0f}% ({names(crashes)}) | {len(crashes_avoided)} of {len(crashes)} sidestepped |
| {sum(holds.values())} HOLD calls | {holds['right']} avoided a {MOVE_EDGE_PCT:.0f}%+ drop, {holds['wrong']} missed a {MOVE_EDGE_PCT:.0f}%+ rally, {holds['neutral']} moved under {MOVE_EDGE_PCT:.0f}% |
| {sum(by_type['BUY'].values())} BUY calls | {by_type['BUY']['right']} of {sum(by_type['BUY'].values())} beat SPY by more than {ALPHA_EDGE_PCT:.0f}% |
| Same ${POSITION_USD:,.0f} per signal, cash results | agent {money(agent_usd)} vs always-buy {money(always_usd)} vs SPY {money(spy_usd)} |

Cost per analysis, measured on the same history: **${sum(per_ticker_cost) / len(per_ticker_cost):.2f} per ticker**; a typical 3-5 ticker run lands near **${sum(per_run_cost) / len(per_run_cost):.2f}** at provider list prices.

Early data, stated plainly: one month of live use and {len(matured)} graded
calls is a small sample. The pattern so far is capital preservation first;
the BUY record is the weak spot, which is exactly what the risk gate's
confidence bar guards.""")


if __name__ == "__main__":
    import sys

    asyncio.run(main("--markdown" in sys.argv))
