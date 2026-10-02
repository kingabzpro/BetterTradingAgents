"""Deterministic `What changed` diffs between repeat analyses (ROADMAP P1.5).

Structured field-by-field comparison - decision, evidence strength, analyst
signals, forecast, data freshness, risk flags. Never an LLM call: the same
inputs must always produce the same lines.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import PreviousCall, StockAnalysis

# Display order matches the evidence cards on the result page.
ANALYST_KEYS = ("market", "technical", "fundamental", "news", "sentiment", "forecast")


def evidence_bucket(confidence: float | None) -> str:
    """Match the UI's convictionLabel thresholds (P1.1/P1.4)."""
    if confidence is None:
        return "unknown"
    pct = round((confidence or 0) * 100)
    if pct >= 70:
        return "strong"
    if pct >= 50:
        return "moderate"
    return "low"


def snapshot(analysis: StockAnalysis) -> PreviousCall:
    """Compact comparable shape of a finished analysis."""
    signals = {}
    for key in ANALYST_KEYS:
        result = getattr(analysis, key, None)
        if result is not None:
            signals[key] = result.signal
    return PreviousCall(
        run_id="",  # filled by the run lookup that stores it
        analyzed_at=None,
        decision=None if analysis.error else analysis.decision,
        confidence=None if analysis.error else analysis.confidence,
        signals=signals,
        forecast_price_5d=analysis.forecast_price_5d,
        as_of=analysis.as_of,
        stale=bool(analysis.data_quality.stale),
        risk_flags=list(analysis.risk_flags),
    )


def _money(value: float | None) -> str:
    return "not recorded" if value is None else f"${value:,.2f}"


def _age_text(age_hours: float | None, stale: bool) -> str:
    if age_hours is None:
        return "not recorded"
    unit = f"{age_hours:.1f}h old" if age_hours < 48 else f"{age_hours / 24:.1f}d old"
    return f"{unit} (stale)" if stale else unit


def _call_age_hours(call: PreviousCall) -> float | None:
    """How old the inputs were at the earlier call, or None if unrecorded."""
    if not call.as_of or call.analyzed_at is None:
        return None
    try:
        stamp = datetime.fromisoformat(call.as_of.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.fromtimestamp(call.analyzed_at, timezone.utc) - stamp).total_seconds() / 3600.0)


def what_changed(current: StockAnalysis, previous: PreviousCall) -> list[str]:
    """One line per compared dimension, changed detail or 'unchanged'."""
    lines: list[str] = []

    decision = None if current.error else current.decision
    if decision == previous.decision:
        lines.append(f"Decision unchanged ({decision or 'n/a'})")
    else:
        lines.append(f"Decision: {previous.decision or 'n/a'} -> {decision or 'n/a'}")

    confidence = None if current.error else current.confidence
    bucket = evidence_bucket(confidence)
    prev_bucket = evidence_bucket(previous.confidence)
    pct = "n/a" if confidence is None else f"{round(confidence * 100)}%"
    prev_pct = "n/a" if previous.confidence is None else f"{round(previous.confidence * 100)}%"
    if bucket == prev_bucket and pct == prev_pct:
        lines.append(f"Evidence strength unchanged ({prev_bucket}, {prev_pct})")
    else:
        lines.append(
            f"Evidence strength: {prev_bucket} {prev_pct} -> {bucket} {pct}"
        )

    cur_signals = snapshot(current).signals
    signal_changes = []
    for key in ANALYST_KEYS:
        before = previous.signals.get(key)
        after = cur_signals.get(key)
        if before == after:
            continue
        signal_changes.append(f"{key} {before or 'not run'} -> {after or 'not run'}")
    if signal_changes:
        lines.append(f"Analyst signals: {'; '.join(signal_changes)}")
    else:
        counts: dict[str, int] = {}
        for signal in cur_signals.values():
            counts[signal] = counts.get(signal, 0) + 1
        detail = ", ".join(f"{n} {name}" for name, n in sorted(counts.items()))
        lines.append(f"Analyst signals unchanged ({detail})" if detail else "Analyst signals unchanged")

    forecast = None if current.error else current.forecast_price_5d
    if forecast == previous.forecast_price_5d:
        lines.append(f"5-day forecast unchanged ({_money(forecast)})")
    else:
        lines.append(f"5-day forecast: {_money(previous.forecast_price_5d)} -> {_money(forecast)}")

    age = current.data_quality.age_hours
    lines.append(
        "Data freshness: "
        f"{_age_text(_call_age_hours(previous), previous.stale)} -> "
        f"{_age_text(age, bool(current.data_quality.stale))}"
    )

    cur_flags = set(current.risk_flags)
    prev_flags = set(previous.risk_flags)
    added = sorted(cur_flags - prev_flags)
    cleared = sorted(prev_flags - cur_flags)
    if not added and not cleared:
        state = f"{len(prev_flags)} open" if prev_flags else "none"
        lines.append(f"Risk flags unchanged ({state})")
    else:
        parts = []
        if added:
            parts.append(f"added {'; '.join(added)}")
        if cleared:
            parts.append(f"cleared {'; '.join(cleared)}")
        lines.append(f"Risk flags: {'; '.join(parts)}")
    return lines


def attach(analysis: StockAnalysis, previous: PreviousCall | None) -> None:
    """Set the analysis' comparison pair in place (cached reruns included)."""
    analysis.previous = previous
    analysis.what_changed = what_changed(analysis, previous) if previous else []


async def previous_call(
    owner_id: str,
    ticker: str,
    before_run_id: str = "",
    before_at: float | None = None,
) -> PreviousCall | None:
    """Newest completed call on `ticker` older than this run's start, or None.

    Runs are listed newest-first, so the first usable result is the previous
    call. A missing `before_at` accepts any earlier run.
    """
    from app import run_history

    ticker = ticker.strip().upper()
    runs = await run_history.list_runs(owner_id, limit=100)
    for item in runs:
        if item.status != "completed" or ticker not in item.tickers:
            continue
        if item.run_id == before_run_id:
            continue
        if before_at is not None and item.started_at >= before_at:
            continue
        status = await run_history.get(item.run_id)
        if status is None:
            continue
        analysis = status.results.get(ticker)
        if analysis is None or analysis.error:
            continue
        call = snapshot(analysis)
        call.run_id = item.run_id
        call.analyzed_at = item.started_at
        return call
    return None
