"""Forecast Analyst: interprets the precomputed 5-day price projections."""

import json

from app.agents import clamp_conf, pick_signal, clip
from app.models import AgentResult, AnalystResult

NAME = "forecast"
DISPLAY = "Forecast Analyst"


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Forecast Analyst",
        goal="Judge what the statistical 5-day price projections actually imply for direction.",
        backstory=(
            "You are a quantitative forecaster. You know statistical projections "
            "are baselines, not guarantees: you weigh them against volatility, "
            "model agreement and the recent trend. All numbers are produced by "
            "Python - you never compute anything yourself, you only interpret."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict):
    from crewai import Task

    return Task(
        description=f"""Interpret the 5-day price projection for stock ticker {ticker}.

Precomputed forecast data (statistical model outputs calculated by Python - do NOT recalculate or invent numbers; a value of null means that model produced no forecast):
{json.dumps(payload, indent=2)}

Two independent projections may be present:
- timegpt_forecast: zero-shot forecast from Nixtla TimeGPT. It is a statistical baseline - one input, never a target or a probability.
- local_trend_forecast: log-linear trend fit over the last ~60 closes. Discount it when forecast_trend_r2 is below 0.25.

Judge whether the projected move is economically meaningful: compare forecast_change_5d_pct of the primary forecast (TimeGPT wins when both exist) against the noise implied by trend_context.volatility_annualized_pct - a small projected move inside the volatility band is noise, not a signal. Note whether the two models agree, and whether the projection direction fits the recent trend context (SMA position, 5d/21d changes). Your call covers the next 5 trading days only.

Respond with ONLY a JSON object, no markdown fences, no text outside the JSON:
{{"ticker": "{ticker}", "signal": "bullish" | "bearish" | "neutral", "confidence": <number 0.0-1.0>, "summary": "<at most 2 sentences citing the key numbers>"}}""",
        expected_output=(
            "A JSON object with keys: ticker, signal (bullish|bearish|neutral), "
            "confidence (0.0-1.0), summary (max 2 sentences)."
        ),
        agent=agent,
        output_pydantic=AnalystResult,
    )


def to_result(data: dict, ticker: str) -> AgentResult:
    return AgentResult(
        agent=NAME,
        signal=pick_signal(data.get("signal")),
        confidence=clamp_conf(data.get("confidence")),
        summary=clip(data.get("summary", ""), 500),
    )


def mock(ticker: str, payload: dict) -> dict:
    """Deterministic rule-based fallback used when no LLM is configured."""
    timegpt = payload.get("timegpt_forecast") or {}
    local = payload.get("local_trend_forecast") or {}
    primary = timegpt or local
    method = primary.get("forecast_method", "none")
    change = primary.get("forecast_change_5d_pct")
    r2 = primary.get("forecast_trend_r2")
    vol = (payload.get("trend_context") or {}).get("volatility_annualized_pct")
    # Annualized vol -> ~1-sigma move over 5 trading days, in percent.
    noise = round(vol / (252**0.5) * (5**0.5), 2) if vol else None

    if method != "timegpt-1" and (r2 is None or r2 < 0.25):
        signal, confidence = "neutral", 0.4
        detail = f"unusable projection ({method}, trend R2 {r2})"
    elif not change:
        signal, confidence = "neutral", 0.45
        detail = "flat projection"
    elif noise and abs(change) < noise:
        signal, confidence = "neutral", 0.45
        detail = f"{change:+.2f}% sits inside the ~{noise:.1f}% 5-day noise band"
    else:
        signal = "bullish" if change > 0 else "bearish"
        ratio = abs(change) / noise if noise else 1.0
        confidence = round(
            min(0.5 + 0.15 * ratio + (0.05 if timegpt else 0.0), 0.8), 2
        )
        detail = f"{change:+.2f}% clears the ~{noise:.1f}% 5-day noise band"
    return {
        "ticker": ticker,
        "signal": signal,
        "confidence": confidence,
        "summary": f"[mock] 5-day forecast {detail} via {method} -> {signal}.",
    }
