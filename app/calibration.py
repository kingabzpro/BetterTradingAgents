"""Confidence calibration from graded outcomes (wiki Roadmap P1.1).

Model confidence means evidence strength, not a probability of profit. This
module grades that meaning against history: mature decisions are grouped by
scope (outlook, depth, model, policy version) and evidence bucket, and each
group reports directional hit rate, alpha stats (BUY/SELL) or missed
upside / avoided downside (HOLD). A configurable minimum sample size (default
30) gates every rate; below it a bucket reports "Track record unavailable".

The manager probability field has its own frozen success event, scored here
with a reliability table and Brier loss. Everything reads SQLite only - no
LLM calls - so a report can be regenerated with one command:

    uv run python -m app.calibration
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone

from app.config import settings

# Bump when decision semantics change materially (manager prompt, risk gate,
# debate structure) so outcomes from different systems are never pooled
# silently. Old rows keep the version they were recorded with.
DECISION_POLICY_VERSION = "2026-10-e"

# Analyst result fields scored in the per-analyst scorecard, and the signal
# vocabulary that counts as a directional call (everything else is a no-call).
ANALYST_FIELDS = (
    "technical",
    "fundamental",
    "news",
    "sentiment",
    "forecast",
    "market",
    "bull",
    "bear",
    "judge",
)
SIGNAL_DIRECTIONS = {
    "bullish": 1,
    "buy": 1,
    "long": 1,
    "bearish": -1,
    "sell": -1,
    "short": -1,
}
ROLLING_WINDOW_DAYS = 90  # analyst record window: recent enough to stay relevant
SCORECARD_LIMIT = 200  # most recent mature decisions considered per lookup

def success_event(horizon_days: int | None = None) -> str:
    """Frozen success event for ManagerResult.probability_beat_spy, built from
    the run's outlook horizon (falling back to the configured default, live-
    applied), so the text never disagrees with how the call grades. Changing
    this definition invalidates every recorded probability; introduce a new
    field instead."""
    return (
        "the call's direction beats SPY over the "
        f"{horizon_days or settings.memory_horizon_days}-day decision-memory horizon: "
        "alpha_vs_spy_pct > 0 for BUY, alpha_vs_spy_pct < 0 for SELL"
    )

# Evidence buckets shown in the UI; keep in sync with convictionLabel in
# static/js/render.js (low < 0.50, moderate < 0.70, strong >= 0.70).
BUCKET_EDGES = ((0.50, "low"), (0.70, "moderate"))
HOLD_MOVE_EDGE_PCT = 2.0  # same edge as memory.lesson: |realized| beyond this is a miss/avoid

PROBABILITY_BANDS = (
    (0.5, "p < 0.50"),
    (0.6, "0.50 - 0.59"),
    (0.7, "0.60 - 0.69"),
    (0.8, "0.70 - 0.79"),
    (0.9, "0.80 - 0.89"),
    (None, "0.90 - 1.00"),
)


def confidence_bucket(confidence: float) -> str:
    for edge, name in BUCKET_EDGES:
        if confidence < edge:
            return name
    return "strong"


def _direction(decision: str) -> int:
    return -1 if decision == "SELL" else 1


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def _fmt_alpha(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def summarize(rows: list[dict], min_observations: int) -> dict:
    """Metrics for one (scope, decision, bucket) group of mature graded rows.

    BUY/SELL need SPY alpha to grade; rows whose SPY fetch failed stay out of
    their group entirely. HOLD needs only the realized return.
    """
    decision = str(rows[0]["decision"]) if rows else "HOLD"
    if decision in ("BUY", "SELL"):
        rows = [row for row in rows if row["alpha_vs_spy_pct"] is not None]
    n = len(rows)
    summary: dict = {"n": n, "available": n >= min_observations}
    if n < min_observations:
        return summary
    if decision in ("BUY", "SELL"):
        alphas = [row["alpha_vs_spy_pct"] for row in rows]
        direction = _direction(decision)
        summary.update(
            directional_hit_rate=sum(1 for a in alphas if direction * a > 0) / n,
            positive_alpha_rate=sum(1 for a in alphas if a > 0) / n,
            mean_alpha_pct=statistics.fmean(alphas),
            median_alpha_pct=statistics.median(alphas),
        )
    else:
        realized = [row["realized_return_pct"] for row in rows]
        summary.update(
            missed_upside_rate=sum(1 for r in realized if r >= HOLD_MOVE_EDGE_PCT) / n,
            avoided_downside_rate=sum(1 for r in realized if r <= -HOLD_MOVE_EDGE_PCT) / n,
            mean_realized_pct=statistics.fmean(realized),
        )
    return summary


def _load_rows(connection: sqlite3.Connection) -> list[dict]:
    try:
        rows = connection.execute(
            "SELECT decision, confidence, outlook, depth, model, policy_version, "
            "realized_return_pct, alpha_vs_spy_pct, manager_probability "
            "FROM decisions "
            "WHERE mature = 1 AND realized_return_pct IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # fresh database: no decisions table yet, so no track record
    return [dict(row) for row in rows]


def track_record(
    decision: str,
    confidence: float,
    outlook: str = "",
    depth: str = "",
    min_observations: int | None = None,
    db_path=None,
) -> dict:
    """Live lookup for one result card: the bucket's historical rates, or
    unavailable when the mature sample is too small. Scope matches the report
    (outlook, depth, current policy version, evidence bucket); rows from
    different models are pooled here and models_pooled says how many."""
    min_n = settings.calibration_min_observations if min_observations is None else min_observations
    bucket = confidence_bucket(confidence)
    path = settings.db_path if db_path is None else db_path
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            row
            for row in _load_rows(connection)
            if row["decision"] == decision
            and confidence_bucket(row["confidence"] or 0.0) == bucket
            and (row["outlook"] or "") == outlook
            and (row["depth"] or "") == depth
            and (row["policy_version"] or "") == DECISION_POLICY_VERSION
        ]
    summary = summarize(rows, min_n)
    summary["decision"] = decision
    summary["confidence_bucket"] = bucket
    summary["min_observations"] = min_n
    summary["models_pooled"] = len({(row["model"] or "unknown") for row in rows})
    return summary


def _probability_score(
    rows: list[dict], min_observations: int
) -> tuple[list[tuple[str, dict]], float | None, int]:
    """Reliability table + Brier loss for manager_probability rows.

    Outcome is 1 when the frozen success event occurred (direction beats SPY),
    0 otherwise; rows without alpha stay out because their outcome is unknown.
    """
    scored = [
        row
        for row in rows
        if row["manager_probability"] is not None and row["alpha_vs_spy_pct"] is not None
    ]

    def outcome(row: dict) -> int:
        return 1 if _direction(str(row["decision"])) * row["alpha_vs_spy_pct"] > 0 else 0

    bands: list[tuple[str, dict]] = []
    lower = 0.0
    for edge, label in PROBABILITY_BANDS:
        band = [
            row
            for row in scored
            if (row["manager_probability"] or 0.0) >= lower
            and (edge is None or (row["manager_probability"] or 0.0) < edge)
        ]
        if edge is not None:
            lower = edge
        if not band:
            continue
        bands.append(
            (
                label,
                {
                    "n": len(band),
                    "available": len(band) >= min_observations,
                    "mean_predicted": statistics.fmean(row["manager_probability"] for row in band),
                    "observed_rate": statistics.fmean(outcome(row) for row in band),
                },
            )
        )
    brier = (
        statistics.fmean(
            (row["manager_probability"] - outcome(row)) ** 2 for row in scored
        )
        if scored
        else None
    )
    return bands, brier, len(scored)


def analyst_scorecard(
    db_path=None,
    window_days: int = ROLLING_WINDOW_DAYS,
    min_observations: int | None = None,
) -> dict:
    """Per-analyst directional record from the live decision memory.

    Joins mature graded decisions with the stored run results
    (analysis_runs.results_json) to recover what each researcher said at
    decision time, then scores every directional call against the realized
    SPY alpha: a bullish call is right when the stock beat SPY, a bearish
    call when it lagged. The rolling window keeps the record recent; the
    minimum-sample gate keeps thin samples from becoming advice. SQLite
    reads only, no LLM calls.
    """
    min_n = (
        settings.calibration_min_observations
        if min_observations is None
        else min_observations
    )
    path = settings.db_path if db_path is None else db_path
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    rows: list = []
    results: dict[str, dict] = {}
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT run_id, ticker, alpha_vs_spy_pct FROM decisions "
                "WHERE mature = 1 AND alpha_vs_spy_pct IS NOT NULL AND run_id != '' "
                "AND date >= ? ORDER BY date DESC LIMIT ?",
                (cutoff, SCORECARD_LIMIT),
            ).fetchall()
            for run_id in {row["run_id"] for row in rows}:
                try:
                    raw = connection.execute(
                        "SELECT results_json FROM analysis_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                except sqlite3.OperationalError:
                    break  # older databases may predate analysis_runs
                if raw is None:
                    continue
                try:
                    results[run_id] = json.loads(raw["results_json"] or "{}")
                except ValueError:
                    continue  # one corrupt run must not sink the scorecard
        except sqlite3.OperationalError:
            pass  # fresh database: no decisions table yet

    alphas: dict[str, list[float]] = {}
    for row in rows:
        result = (results.get(row["run_id"]) or {}).get(row["ticker"])
        if not isinstance(result, dict):
            continue
        alpha = row["alpha_vs_spy_pct"]
        for key in ANALYST_FIELDS:
            agent = result.get(key)
            if not isinstance(agent, dict):
                continue
            direction = SIGNAL_DIRECTIONS.get(
                str(agent.get("signal") or "").strip().lower(), 0
            )
            if direction:
                alphas.setdefault(key, []).append(direction * alpha)

    analysts: dict[str, dict] = {}
    for name, values in sorted(alphas.items()):
        n = len(values)
        analysts[name] = {
            "n": n,
            "available": n >= min_n,
            "hit_rate_pct": round(sum(1 for v in values if v > 0) / n * 100, 1),
            "mean_alpha_pct": round(statistics.fmean(values), 2),
        }
    return {"window_days": window_days, "min_observations": min_n, "analysts": analysts}


def scorecard_text(scorecard: dict) -> str:
    """One compact block for the judge prompt (never invent a record)."""
    analysts = scorecard.get("analysts") or {}
    available = {name: stats for name, stats in analysts.items() if stats["available"]}
    if not available:
        return (
            "none yet - no analyst has a mature record at the "
            f"{scorecard.get('min_observations', '?')}-call minimum "
            f"(window {scorecard.get('window_days', '?')}d)"
        )
    parts = [
        f"{name}: {stats['hit_rate_pct']:.0f}% directional hit rate "
        f"(n={stats['n']}, mean alpha {stats['mean_alpha_pct']:+.2f}%)"
        for name, stats in available.items()
    ]
    text = "; ".join(parts)
    thin = [name for name in analysts if name not in available]
    if thin:
        text += f". Below the minimum sample: {', '.join(thin)}"
    return text


def _band_for(predicted: float) -> tuple[float, float | None]:
    """The PROBABILITY_BANDS window containing `predicted`."""
    lower = 0.0
    for edge, _label in PROBABILITY_BANDS:
        if predicted >= lower and (edge is None or predicted < edge):
            return lower, edge
        if edge is not None:
            lower = edge
    return 0.0, None


def sizing_multiplier_for(
    predicted: float | None, db_path=None, min_observations: int | None = None
) -> tuple[float, str | None]:
    """Item 19: scale a proposed risk by the manager's own reliability curve.

    The band containing `predicted` reports its observed success rate over
    mature graded decisions; the multiplier is observed/predicted, clamped to
    [0.5, 1.0] - calibration can only shrink a position, never inflate it,
    and the risk gate still caps the result. Below the minimum sample the
    function returns 1.0 (no adjustment) rather than inventing a curve from
    thin data. Returns (multiplier, note); note is None when nothing binds.
    """
    min_n = (
        settings.calibration_min_observations
        if min_observations is None
        else min_observations
    )
    if predicted is None or predicted <= 0:
        return 1.0, None
    path = settings.db_path if db_path is None else db_path
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = _load_rows(connection)
    lower, upper = _band_for(predicted)
    band = [
        row
        for row in rows
        if row["manager_probability"] is not None
        and row["alpha_vs_spy_pct"] is not None
        and row["manager_probability"] >= lower
        and (upper is None or row["manager_probability"] < upper)
    ]
    n = len(band)
    if n < min_n:
        return 1.0, None
    observed = (
        sum(
            1
            if _direction(str(row["decision"])) * row["alpha_vs_spy_pct"] > 0
            else 0
            for row in band
        )
        / n
    )
    multiplier = round(min(1.0, max(0.5, observed / predicted)), 2)
    if multiplier >= 1.0:
        return 1.0, None
    return multiplier, (
        f"calibration sizing x{multiplier:.2f}: band observed {observed:.0%} "
        f"vs predicted {predicted:.0%} (n={n})"
    )


def build_report(db_path=None, min_observations: int | None = None) -> str:
    """Full calibration report as markdown. SQLite reads only, no LLM calls."""
    min_n = settings.calibration_min_observations if min_observations is None else min_observations
    path = settings.db_path if db_path is None else db_path
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = _load_rows(connection)

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        scope = (
            row["outlook"] or "unknown outlook",
            row["depth"] or "unknown depth",
            row["model"] or "unknown model",
            row["policy_version"] or "unversioned",
        )
        key = (scope, str(row["decision"]), confidence_bucket(row["confidence"] or 0.0))
        groups.setdefault(key, []).append(row)

    lines = [
        "# Confidence calibration report",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} from `{path}` - SQLite only, no LLM calls.",
        "",
        f"- Decision policy version: `{DECISION_POLICY_VERSION}` (current); older rows keep the version they were recorded with.",
        f"- Manager probability success event: {success_event()}.",
        f"- Minimum mature observations per bucket: {min_n} (`CALIBRATION_MIN_OBSERVATIONS`).",
        "- `confidence` means evidence strength, never a probability of profit.",
        "",
        "## Positioned decisions (BUY / SELL)",
        "",
        "| Scope | Decision | Evidence bucket | n mature | Directional hit rate | Positive-alpha rate | Mean alpha | Median alpha |",
        "|---|---|---|---|---|---|---|---|",
    ]
    positioned = sorted(
        ((key, rows_) for key, rows_ in groups.items() if key[1] in ("BUY", "SELL")),
        key=lambda item: (item[0][0], item[0][1], item[0][2]),
    )
    for (scope, decision, bucket), group in positioned:
        stats = summarize(group, min_n)
        scope_text = " / ".join(scope)
        if not stats["available"]:
            lines.append(
                f"| {scope_text} | {decision} | {bucket} | {stats['n']} "
                f"| Track record unavailable (n={stats['n']} < {min_n}) | - | - | - |"
            )
        else:
            lines.append(
                f"| {scope_text} | {decision} | {bucket} | {stats['n']} "
                f"| {_fmt_pct(stats['directional_hit_rate'])} | {_fmt_pct(stats['positive_alpha_rate'])} "
                f"| {_fmt_alpha(stats['mean_alpha_pct'])} | {_fmt_alpha(stats['median_alpha_pct'])} |"
            )

    lines += [
        "",
        "## HOLD decisions",
        "",
        "No win rate is invented for HOLD: missed upside and avoided downside describe what standing aside did.",
        "",
        "| Scope | Evidence bucket | n mature | Missed upside | Avoided downside | Mean realized |",
        "|---|---|---|---|---|---|",
    ]
    holds = sorted(
        ((key, rows_) for key, rows_ in groups.items() if key[1] == "HOLD"),
        key=lambda item: (item[0][0], item[0][1]),
    )
    for (scope, _decision, bucket), group in holds:
        stats = summarize(group, min_n)
        scope_text = " / ".join(scope)
        if not stats["available"]:
            lines.append(
                f"| {scope_text} | {bucket} | {stats['n']} "
                f"| Track record unavailable (n={stats['n']} < {min_n}) | - | - |"
            )
        else:
            lines.append(
                f"| {scope_text} | {bucket} | {stats['n']} "
                f"| {_fmt_pct(stats['missed_upside_rate'])} | {_fmt_pct(stats['avoided_downside_rate'])} "
                f"| {_fmt_alpha(stats['mean_realized_pct'])} |"
            )

    bands, brier, scored_n = _probability_score(rows, min_n)
    lines += [
        "",
        "## Manager probability (reliability)",
        "",
        f"`probability_beat_spy` is scored against its frozen success event with n={scored_n} mature positioned decisions.",
        "",
        "| Predicted band | n | Mean predicted | Observed hit rate |",
        "|---|---|---|---|",
    ]
    for label, stats in bands:
        observed = (
            _fmt_pct(stats["observed_rate"])
            if stats["available"]
            else f"Track record unavailable (n={stats['n']} < {min_n})"
        )
        lines.append(
            f"| {label} | {stats['n']} | {stats['mean_predicted']:.2f} | {observed} |"
        )
    lines += [
        "",
        f"Brier score: {'n/a' if brier is None else f'{brier:.4f}'} (lower is better; a constant 0.5 prediction scores 0.25).",
    ]

    scorecard = analyst_scorecard(db_path, min_observations=min_n)
    lines += [
        "",
        "## Analyst scorecard",
        "",
        f"Every directional analyst call recovered from the stored runs and scored "
        f"against realized SPY alpha, rolling {scorecard['window_days']}-day window; "
        f"minimum {min_n} mature calls per analyst.",
        "",
        "| Analyst | Directional calls | Hit rate | Mean alpha |",
        "|---|---|---|---|",
    ]
    for name, stats in scorecard["analysts"].items():
        if stats["available"]:
            lines.append(
                f"| {name} | {stats['n']} | {_fmt_pct(stats['hit_rate_pct'] / 100)} "
                f"| {_fmt_alpha(stats['mean_alpha_pct'])} |"
            )
        else:
            lines.append(
                f"| {name} | {stats['n']} | Track record unavailable "
                f"(n={stats['n']} < {min_n}) | - |"
            )
    if not scorecard["analysts"]:
        lines.append("| - | 0 | no mature graded decisions in the window | - |")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    from app import memory

    memory._init_db()  # a fresh database has no decisions table yet
    print(build_report())
