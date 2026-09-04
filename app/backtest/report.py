"""JSON + markdown report writer for backtests (ROADMAP 2.2).

Reports carry the honesty flags the roadmap demands: the LLM memorization
risk on historical dates, the current-vintage fundamentals bias, and the
point-in-time news rule.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.grade import BacktestResult

MEMORIZATION_NOTE = (
    "LLM-mode backtests on dates inside the model's training data are inflated: "
    "the model may already know how each window resolved. Prefer mock mode or "
    "very recent dates for headline numbers."
)


def build_flags(mode: str) -> dict:
    return {
        "memorization_risk": "high" if mode == "llm" else "low (mock mode - no LLM)",
        "memorization_note": MEMORIZATION_NOTE if mode == "llm" else "",
        "fundamentals_bias": (
            "fundamentals are current-vintage, not point-in-time - a known bias, "
            "stated here per ROADMAP 2.2"
        ),
        "news_rule": "news items filtered to published <= decision date",
    }


def result_payload(result: BacktestResult) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": result.config,
        "flags": result.flags,
        "overall": result.overall,
        "tickers": result.tickers,
        "outcomes": [vars(outcome) for outcome in result.outcomes],
        "ungraded": [vars(decision) for decision in result.ungraded],
    }


def write_report(result: BacktestResult, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"report-{result.config['mode']}.json"
    md_path = out_dir / f"report-{result.config['mode']}.md"

    payload = result_payload(result)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")
    return json_path, md_path


def _fmt(value, signed: bool = True) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        sign = "+" if (signed and value >= 0) else ""
        return f"{sign}{value:.2f}"
    return str(value)


def _pct(value, signed: bool = True) -> str:
    return "-" if value is None else _fmt(value, signed) + "%"


def _markdown(payload: dict) -> str:
    config = payload["config"]
    flags = payload["flags"]
    lines = [
        f"# Backtest report - {config['mode']} mode",
        "",
        f"Generated {payload['generated_at'][:19]}Z",
        "",
        "| Setting | Value |",
        "|---|---|",
        f"| Mode | {config['mode']} ({config.get('model', 'rule-based mock')}) |",
        f"| Tickers | {', '.join(config['tickers'])} |",
        f"| Grid | {config['start']} to {config['end']} every {config['step_days']}d |",
        f"| Horizon | {config['horizon_days']} days |",
        f"| Depth | {config['depth']} |",
        f"| Outlook | {config['outlook']} |",
        f"| Round-trip cost | {config['cost_pct']:.2f}% |",
        f"| Short selling | {'enabled' if config['short'] else 'disabled (SELL scores 0)'} |",
        "",
        "## Flags",
        "",
        f"- memorization risk: **{flags['memorization_risk']}**"
        + (f" - {flags['memorization_note']}" if flags["memorization_note"] else ""),
        f"- {flags['fundamentals_bias']}",
        f"- {flags['news_rule']}",
        "",
        "## Results",
        "",
        "| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    rows = [(ticker, metrics) for ticker, metrics in payload["tickers"].items()]
    rows.append(("overall", payload["overall"]))
    for name, m in rows:
        counts = m["counts"]
        lines.append(
            f"| {name} | {m['decisions']} | {counts['BUY']} / {counts['SELL']} / "
            f"{counts['HOLD']} | {_pct(m['hit_rate_pct'], signed=False)} | "
            f"{_pct(m['avg_net_pct'])} | {_pct(m['avg_alpha_pct'])} | "
            f"{_pct(m['cumulative_pct'])} | {m['sharpe']} | "
            f"{_pct(m['max_drawdown_pct'], signed=False)} | "
            f"{_pct(m.get('buy_hold_pct'))} |"
        )

    lines += [
        "",
        "## Decisions",
        "",
        "| Ticker | Date | Decision | Conf. | Entry | Exit | Net | SPY | Alpha |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for o in payload["outcomes"]:
        lines.append(
            f"| {o['ticker']} | {o['date']} | {o['decision']} | {o['confidence']:.2f} "
            f"| {o['entry']:.2f} | {o['exit']:.2f} | {_pct(o['net_pct'])} | "
            f"{_pct(o['spy_pct'])} | {_pct(o['alpha_pct'])} |"
        )
    if payload["ungraded"]:
        lines += [
            "",
            f"Ungraded (window not finished): "
            + ", ".join(f"{d['ticker']}@{d['date']}" for d in payload["ungraded"]),
        ]
    lines += [
        "",
        "---",
        "Educational project - these numbers describe a simulation of a "
        "rule-based or LLM pipeline, not investment advice.",
        "",
    ]
    return "\n".join(lines)
