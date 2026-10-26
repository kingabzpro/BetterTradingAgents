"""JSON + markdown report writer for backtests (ROADMAP 2.2, P1.2).

Reports carry the honesty flags the roadmap demands: the LLM memorization
risk on historical dates, the fundamentals vintage actually replayed, and the
point-in-time news rule. Baselines (all-HOLD, buy-and-hold, deterministic
momentum) and - when a holdout split exists - per-period sample sizes,
bootstrap intervals, and promotion verdicts are part of every report. Each
report also carries the manager-probability reliability table (Brier score)
and the per-analyst scorecard for the graded run.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.backtest.grade import BacktestResult

# Decisions this close to today are treated as forward windows: the outcome
# window is recent enough that memorized training data is a smaller concern
# (never zero - the label says which side of the line a run sits on).
FORWARD_WINDOW_DAYS = 45

MEMORIZATION_NOTE = (
    "LLM-mode backtests on dates inside the model's training data are inflated: "
    "the model may already know how each window resolved. Prefer mock mode or "
    "very recent dates for headline numbers."
)

CLAIMS_POLICY = (
    "Headline performance claims belong to forward paper-trading windows "
    "(recent live runs on unresolved outcomes). Historical replays are "
    "qualitative evidence only."
)

CURRENT_FUNDAMENTALS_WARNING = (
    "**WARNING: current-vintage fundamentals were replayed on historical dates. "
    "This leaks information that did not exist at decision time; these numbers "
    "are not evidence of skill and must not be quoted as headline results.**"
)


def _window_label(end: str | None) -> str:
    """Historical vs forward window from the grid end date (unknown = historical,
    the conservative side: it keeps the memorization warning on)."""
    if not end:
        return "unknown (treated as historical)"
    try:
        last = date.fromisoformat(end)
    except ValueError:
        return "unknown (treated as historical)"
    if last >= date.today() - timedelta(days=FORWARD_WINDOW_DAYS):
        return "forward"
    return "historical"


def build_flags(
    mode: str, fundamentals: str = "excluded", start: str | None = None, end: str | None = None
) -> dict:
    if fundamentals == "current":
        fundamentals_bias = (
            "current-vintage fundamentals replayed - LOOK-AHEAD BIAS, opt-in only"
        )
    elif fundamentals == "sec_edgar":
        fundamentals_bias = (
            "fundamentals served as filed by SEC EDGAR on or before each date - "
            "point-in-time, no look-ahead"
        )
    else:
        fundamentals_bias = (
            "fundamentals excluded from replay: opt in to the point-in-time "
            "SEC EDGAR source with --sec-edgar-fundamentals"
        )
    window = _window_label(end)
    if mode != "llm":
        memorization_risk = "low (mock mode - no LLM)"
    elif window == "forward":
        memorization_risk = "reduced (forward window - recent dates)"
    else:
        memorization_risk = "high"
    return {
        "window": window,
        "claims_policy": CLAIMS_POLICY,
        "memorization_risk": memorization_risk,
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


def _usage_text(usage: dict | None) -> str:
    """One-line LLM token/cost summary for the settings table (item 8 A/B)."""
    if not usage or not usage.get("total_tokens"):
        return "no LLM calls (mock mode)"
    cost = usage.get("cost_usd") or 0.0
    priced = usage.get("priced_runs") or 0
    suffix = f"~${cost:.4f} across {priced} priced run(s)" if priced else "unpriced"
    return (
        f"{usage['prompt_tokens']:,} prompt + {usage['completion_tokens']:,} "
        f"completion = {usage['total_tokens']:,} tokens · {suffix}"
    )


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
        f"| LLM temperature | {config.get('temperature', 'not recorded')} |",
        f"| Tickers | {', '.join(config['tickers'])} |",
        f"| Grid | {config['start']} to {config['end']} every {config['step_days']}d |",
        f"| Horizon | {config['horizon_days']} days |",
        f"| Depth | {config['depth']}"
        + (f" (excluded: {', '.join(config['excluded_analysts'])})" if config.get("excluded_analysts") else "")
        + " |",
        f"| Outlook | {config['outlook']} |",
        f"| Fundamentals | {flags['fundamentals_vintage']} |",
        f"| Round-trip cost | base {config['cost_pct']:.2f}% + participation/volatility term (item 17) |",
        f"| Risk regime | {'enabled (item 18)' if config.get('risk_regime_enabled') else 'off'} |",
        f"| Short selling | {'enabled' if config['short'] else 'disabled (SELL is cash, graded on relative weakness vs SPY)'} |",
        "| Portfolio accounting | equal weight per decision date, compounded |",
        f"| Benchmarks (buy & hold) | SPY {_pct(overall.get('spy_cumulative_pct'))} · "
        f"QQQ {_pct(overall.get('qqq_cumulative_pct'))} |",
        f"| LLM usage | {_usage_text(overall.get('llm_usage'))} |",
        f"| Decision policy | `{config.get('policy_version', '')}` · debate rounds {config.get('debate_rounds', '')} |",
        f"| Manifest | `manifest-{config.get('name', config['mode'])}.json` (revision, data hashes, seeds) |",
        "",
    ]
    if flags["fundamentals_vintage"] == "current":
        lines += ["## Look-ahead warning", "", CURRENT_FUNDAMENTALS_WARNING, ""]
    lines += [
        "## Flags",
        "",
        f"- window: **{flags['window']}** - {flags['claims_policy']}",
        f"- memorization risk: **{flags['memorization_risk']}**"
        + (f" - {flags['memorization_note']}" if flags["memorization_note"] else ""),
        f"- {flags['fundamentals_bias']}",
        f"- {flags['news_rule']}",
        "",
        "## Results",
        "",
        "Hit rate counts BUY and SELL calls; a long-only SELL is graded on relative "
        "weakness vs SPY (right when the stock underperforms), so SELLs are no longer "
        "invisible. Cumulative, Sharpe, and max drawdown compound the equal-weight "
        "per-date portfolio.",
        "",
        "| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    rows = [(ticker, metrics) for ticker, metrics in payload["tickers"].items()]
    rows.append(("overall", overall))
    for name, m in rows:
        lines.append(_metrics_row(name, m))

    calibration = overall.get("calibration")
    if calibration:
        brier = calibration.get("brier")
        lines += [
            "",
            "## Manager probability calibration",
            "",
            f"`probability_beat_spy` scored against its frozen success event "
            f"({calibration['event']}); n={calibration['n']} gradable positioned calls. "
            f"Brier score: {'n/a' if brier is None else f'{brier:.4f}'} "
            "(lower is better; a constant 0.5 prediction scores 0.25).",
            "",
            "| Predicted band | n | Mean predicted | Observed rate |",
            "|---|---|---|---|",
        ]
        for band in calibration["bands"]:
            observed = (
                _pct(band["observed_rate"] * 100, signed=False)
                if band["available"]
                else f"Track record unavailable (n={band['n']} < {calibration['min_observations']})"
            )
            lines.append(
                f"| {band['label']} | {band['n']} | {band['mean_predicted']:.2f} | {observed} |"
            )

    scorecard = overall.get("analyst_scorecard")
    if scorecard:
        lines += [
            "",
            "## Analyst scorecard",
            "",
            "Each researcher's directional calls graded against SPY over the horizon "
            "(neutral signals are no-calls and stay out).",
            "",
            "| Analyst | Directional calls | Hit rate | Mean alpha |",
            "|---|---|---|---|",
        ]
        for analyst, stats in scorecard.items():
            lines.append(
                f"| {analyst} | {stats['directional_calls']} | "
                f"{_pct(stats['hit_rate_pct'], signed=False)} | {_pct(stats['mean_alpha_pct'])} |"
            )

    stop_replay = overall.get("stop_replay")
    if stop_replay:
        lines += [
            "",
            "## Stop overlay replay",
            "",
            f"What the live stop rules (absolute -{stop_replay['stop_loss_pct']:.1f}%, "
            f"trailing -{stop_replay['trailing_stop_pct']:.1f}% from peak) would have done "
            f"across the {stop_replay['buy_decisions']} graded BUY decisions in this sample: "
            f"**{stop_replay['stopped']} exited early** "
            f"({stop_replay['absolute_stops']} absolute, {stop_replay['trailing_stops']} trailing). "
            f"Mean net delta vs holding to the horizon: "
            f"{_pct(stop_replay['mean_delta_pct'])} (sum {_pct(stop_replay['sum_delta_pct'])}).",
            "",
            "Daily closes only (triggers read at the close, conservative across gaps), "
            "no cooldown or re-entry modeling, same round-trip cost on both paths.",
        ]

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
        "| Ticker | Date | Decision | Conf. | Prob. | Weight | Entry | Exit | Cost | Net | SPY | Alpha | Regime |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for o in payload["outcomes"]:
        probability = o.get("manager_probability")
        weight = o.get("target_weight_pct")
        lines.append(
            f"| {o['ticker']} | {o['date']} | {o['decision']} | {o['confidence']:.2f} "
            f"| {'-' if probability is None else f'{probability:.2f}'} "
            f"| {'-' if weight is None else f'{weight:.1f}%'} "
            f"| {o['entry']:.2f} | {o['exit']:.2f} | {_pct(o.get('cost_pct'), signed=False)} "
            f"| {_pct(o['net_pct'])} | "
            f"{_pct(o['spy_pct'])} | {_pct(o['alpha_pct'])} "
            f"| {o.get('risk_regime') or '-'} |"
        )
    if payload["ungraded"]:
        lines += [
            "",
            "Ungraded (window not finished): "
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
