"""A/B regression panel (phase 4, item 15).

One command per arm: runs the fixed panel grid through the backtest harness
and appends a compact entry to backtests/ab-ledger.json. A second command
prints the comparison table: decision flips, token and cost deltas, and the
same-config noise band when a control arm exists.

    uv run python scripts/ab_panel.py run --name votes3-a --votes 3
    uv run python scripts/ab_panel.py run --name votes3-b --votes 3
    uv run python scripts/ab_panel.py compare --a votes3-a --b votes3-b

Grid defaults match the release A/Bs (NVDA,AMD,MSFT x 2025-06-02..07-14,
pro depth) so entries stay comparable across sessions. Environment overrides
(--votes, --temperature) are set before app.config is imported, so they reach
the settings object the same way an operator would.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO / "backtests" / "ab-ledger.json"

PANEL_DEFAULTS = {
    "tickers": "NVDA,AMD,MSFT",
    "start": "2025-06-02",
    "end": "2025-07-14",
    "step": 21,
    "horizon": 21,
    "depth": "pro",
}


# ---- pure helpers (kept import-clean for the hermetic check) ----------------


def flips(a: dict[str, str], b: dict[str, str]) -> list[tuple[str, str, str]]:
    """Decisions that differ between two arms, as (key, a_decision, b_decision)."""
    keys = sorted(set(a) | set(b))
    return [(key, a.get(key, "-"), b.get(key, "-")) for key in keys if a.get(key) != b.get(key)]


def load_ledger(path: Path = LEDGER_PATH) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []


def save_entry(entry: dict, path: Path = LEDGER_PATH) -> None:
    """Upsert by name so a rerun replaces its own row instead of duplicating."""
    entries = [e for e in load_ledger(path) if e.get("name") != entry["name"]]
    entries.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def same_config(a: dict, b: dict) -> bool:
    keys = ("mode", "depth", "votes", "temperature", "tickers", "start", "end")
    return all(a.get(key) == b.get(key) for key in keys)


def render_comparison(a: dict, b: dict, control: dict | None = None) -> str:
    """The report: per-decision table with DIFF markers, then the summary."""
    a_decisions, b_decisions = a.get("decisions", {}), b.get("decisions", {})
    lines = [
        f"# A/B: {a['name']} vs {b['name']}",
        "",
        f"- {a['name']}: mode={a.get('mode')} depth={a.get('depth')} votes={a.get('votes')} "
        f"temperature={a.get('temperature')}",
        f"- {b['name']}: mode={b.get('mode')} depth={b.get('depth')} votes={b.get('votes')} "
        f"temperature={b.get('temperature')}",
        "",
        "| Decision | " + a["name"] + " | " + b["name"] + " | |",
        "|---|---|---|---|",
    ]
    for key, a_decision, b_decision in flips(a_decisions, b_decisions):
        lines.append(f"| {key} | {a_decision} | {b_decision} | DIFF |")
    differences = flips(a_decisions, b_decisions)
    total = len(set(a_decisions) | set(b_decisions))
    lines += [
        "",
        f"Decision flips: **{len(differences)}/{total}**",
    ]
    if control is not None:
        control_flips = flips(control.get("decisions", {}), a_decisions)
        lines.append(
            f"Control ({control['name']}, same config as {a['name']}): "
            f"{len(control_flips)}/{total} flips = the noise band this panel has to beat"
        )
    lines += [
        "",
        "| Arm | Total tokens | Cost |",
        "|---|---|---|",
        f"| {a['name']} | {a.get('tokens', 0):,} | ${a.get('cost_usd', 0)} |",
        f"| {b['name']} | {b.get('tokens', 0):,} | ${b.get('cost_usd', 0)} |",
    ]
    return "\n".join(lines)


# ---- the run arm ------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    # Environment first: app.config reads it at import time.
    os.environ["MANAGER_VOTES"] = str(args.votes)
    if args.temperature is not None:
        os.environ["LLM_TEMPERATURE"] = str(args.temperature)

    sys.path.insert(0, str(REPO))
    from app.config import settings  # noqa: E402
    from app.backtest.run import run_backtest  # noqa: E402

    import asyncio

    result = asyncio.run(
        run_backtest(
            tickers=[t.strip().upper() for t in args.tickers.split(",") if t.strip()],
            start=args.start,
            end=args.end,
            step_days=args.step,
            horizon_days=args.horizon,
            depth=args.depth,
            mode=args.mode,
            name=args.name,
        )
    )
    usage = result.overall.get("llm_usage", {})
    entry = {
        "name": args.name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": args.mode,
        "depth": args.depth,
        "votes": settings.manager_votes,
        "temperature": settings.llm_temperature,
        "model": result.config.get("model", ""),
        "tickers": args.tickers,
        "start": args.start,
        "end": args.end,
        "tokens": usage.get("total_tokens", 0),
        "cost_usd": usage.get("cost_usd", 0.0),
        "decisions": {
            f"{o.ticker}@{o.date}": o.decision for o in result.outcomes
        },
    }
    save_entry(entry)
    print(f"run '{args.name}': {len(entry['decisions'])} decisions, "
          f"{entry['tokens']:,} tokens, ${entry['cost_usd']}")
    print(f"ledger: {LEDGER_PATH}")

    # Instant feedback: flips against the newest same-config run, if any.
    prior = [
        e for e in load_ledger()
        if e["name"] != args.name and same_config(e, entry)
    ]
    if prior:
        control = prior[-1]
        diffs = flips(control["decisions"], entry["decisions"])
        print(f"same-config control '{control['name']}': {len(diffs)} flips "
              f"of {len(entry['decisions'])} decisions")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    entries = {e["name"]: e for e in load_ledger()}
    for name in (args.a, args.b):
        if name not in entries:
            print(f"unknown ledger entry: {name}", file=sys.stderr)
            return 2
    control = entries.get(args.control) if args.control else None
    print(render_comparison(entries[args.a], entries[args.b], control))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ab_panel.py")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run one arm on the panel grid")
    run_p.add_argument("--name", required=True)
    run_p.add_argument("--mode", default="llm", choices=["llm", "mock"])
    run_p.add_argument("--depth", default=PANEL_DEFAULTS["depth"])
    run_p.add_argument("--tickers", default=PANEL_DEFAULTS["tickers"])
    run_p.add_argument("--start", default=PANEL_DEFAULTS["start"])
    run_p.add_argument("--end", default=PANEL_DEFAULTS["end"])
    run_p.add_argument("--step", type=int, default=PANEL_DEFAULTS["step"])
    run_p.add_argument("--horizon", type=int, default=PANEL_DEFAULTS["horizon"])
    run_p.add_argument("--votes", type=int, default=1)
    run_p.add_argument("--temperature", type=float, default=None)
    run_p.set_defaults(func=cmd_run)

    cmp_p = sub.add_parser("compare", help="print the comparison table")
    cmp_p.add_argument("--a", required=True)
    cmp_p.add_argument("--b", required=True)
    cmp_p.add_argument("--control", default=None)
    cmp_p.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
