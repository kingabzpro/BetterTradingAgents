"""JSON + markdown report writer for backtests (ROADMAP 2.2, P1.2).

Reports carry the honesty flags the roadmap demands: the LLM memorization
risk on historical dates, the fundamentals vintage actually replayed, and the
point-in-time news rule. Baselines (all-HOLD, buy-and-hold, deterministic
momentum) and - when a holdout split exists - per-period sample sizes,
bootstrap intervals, and promotion verdicts are part of every report.
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

CURRENT_FUNDAMENTALS_WARNING = (
    "**WARNING: current-vintage fundamentals were replayed on historical dates. "
    "This leaks information that did not exist at decision time; these numbers "
    "are not evidence of skill and must not be quoted as headline results.**"
)


def build_flags(mode: str, fundamentals: str = "excluded") -> dict:
    if fundamentals == "current":
        fundamentals_bias = (
            "current-vintage fundamentals replayed - LOOK-AHEAD BIAS, opt-in only"
        )
    else:
        fundamentals_bias = (
            "fundamentals excluded from replay: no point-in-time source exists "
            "(opt back in with --allow-current-fundamentals and accept the bias)"
        )
    return {
        "memorization_risk": "high" if mode == "llm" else "low (mock mode - no LLM)",
        "memorization_note": MEMORIZATION_NOTE if mode == "llm" else "",
        "fundamentals_vintage": fundamentals,
        "fundamentals_bias": fundamentals_bias,
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


def write_report(
    result: BacktestResult, out_dir: Path, name: str | None = None
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = name or result.config["mode"]
    json_path = out_dir / f"report-{name}.json"
    md_path = out_dir / f"report-{name}.md"

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


def _ci_text(ci) -> str:
    return "-" if ci is None else f"[{_fmt(ci[0])}%, {_fmt(ci[1])}%]"


def _metrics_row(name: str, m: dict) -> str:
    counts = m["counts"]
    return (
        f"| {name} | {m['decisions']} | {counts['BUY']} / {counts['SELL']} / "
        f"{counts['HOLD']} | {_pct(m['hit_rate_pct'], signed=False)} | "
        f"{_pct(m['avg_net_pct'])} | {_pct(m['avg_alpha_pct'])} | "
        f"{_pct(m['cumulative_pct'])} | {m['sharpe']} | "
        f"{_pct(m['max_drawdown_pct'], signed=False)} | "
        f"{_pct(m.get('buy_hold_pct'))} |"
    )


def _period_row(label: str, stats: dict) -> str:
    return (
        f"| {label} | {stats['decisions']} | {stats['positioned_n']} | "
        f"{_pct(stats['avg_alpha_pct'])} | {_ci_text(stats['alpha_ci_pct'])} |"
    )


def _markdown(payload: dict) -> str:
    config = payload["config"]
    flags = payload["flags"]
    overall = payload["overall"]
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
        f"| Depth | {config['depth']}"
        + (f" (excluded: {', '.join(config['excluded_analysts'])})" if config.get("excluded_analysts") else "")
        + " |",
        f"| Outlook | {config['outlook']} |",
        f"| Fundamentals | {flags['fundamentals_vintage']} |",
        f"| Round-trip cost | {config['cost_pct']:.2f}% |",
        f"| Short selling | {'enabled' if config['short'] else 'disabled (SELL scores 0)'} |",
        f"| Decision policy | `{config.get('policy_version', '')}` · debate rounds {config.get('debate_rounds', '')} |",
        f"| Manifest | `manifest-{config.get('name', config['mode'])}.json` (revision, data hashes, seeds) |",
        "",
    ]
    if flags["fundamentals_vintage"] == "current":
        lines += ["## Look-ahead warning", "", CURRENT_FUNDAMENTALS_WARNING, ""]
    lines += [
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
    rows.append(("overall", overall))
    for name, m in rows:
        lines.append(_metrics_row(name, m))

    baselines = overall.get("baselines") or {}
    if baselines:
        momentum = baselines.get("momentum", {})
        lines += [
            "",
            "## Baselines (the pipeline must beat a cheap baseline to justify its cost)",
            "",
            "| Baseline | Decisions | Positioned | Avg alpha | Cumulative | Note |",
            "|---|---|---|---|---|---|",
            f"| all HOLD | {baselines['all_hold']['decisions']} | - | - | 0.00% | "
            f"{baselines['all_hold']['note']} |",
            f"| deterministic momentum | {momentum.get('decisions', 0)} | "
            f"{momentum.get('positioned_n', 0)} | "
            f"{_pct(momentum.get('avg_alpha_pct'))} | {_pct(momentum.get('cumulative_pct'))} | "
            "63-day skip-month momentum, volatility-scaled (same costs) |",
            "| buy & hold | per-ticker column above | - | - | per-ticker column | "
            "hold each ticker across the graded span |",
        ]

    periods = overall.get("periods")
    if periods:
        lines += [
            "",
            "## Tune / test split",
            "",
            f"Holdout {config.get('holdout')}: dates before it tuned anything, "
            "dates on/after it were untouched.",
            "",
            "| Period | Decisions | Positioned (alpha) | Mean alpha | 95% bootstrap CI |",
            "|---|---|---|---|---|",
            _period_row("tune", periods["tune"]),
            _period_row("test (holdout)", periods["test"]),
            "",
            f"- tune verdict: {periods['tune']['verdict']}.",
            f"- test verdict: {periods['test']['verdict']}.",
        ]

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
