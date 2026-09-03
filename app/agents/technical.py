"""Technical Analyst: interprets indicators that Python already computed."""

import json

from app.agents import clamp_conf, first_sentence, pick_signal, clip
from app.models import AgentResult, AnalystResult

NAME = "technical"
DISPLAY = "Technical Analyst"


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Technical Analyst",
        goal="Judge the short-term price direction of a stock from precomputed technical indicators.",
        backstory=(
            "You are an experienced market technician. You trust price action, moving "
            "averages, momentum and volatility. All numbers are calculated by Python - "
            "you never compute anything yourself, you only interpret what the data says."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict):
    from crewai import Task

    return Task(
        description=f"""Analyze the technical picture of stock ticker {ticker}.

Precomputed indicators (calculated by Python from 6 months of daily closes - do NOT recalculate or invent numbers):
{json.dumps(payload, indent=2)}

Weigh trend (price vs SMA20/SMA50, SMA20 vs SMA50), momentum (10d momentum, 21d/63d changes, MACD histogram), RSI (overbought >70, oversold <30), Bollinger position (percent_b near 1.0 = stretched upper band, near 0.0 = lower band), volatility and ATR, and volume confirmation (relative_volume >1.5 = unusual interest, check whether it supports or opposes the move). Treat forecast_price_5d as low-weight supporting evidence, not a guaranteed target or probability. When forecast_method is log_linear_trend, discount it if forecast_trend_r2 is below 0.25.

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
    score = 0.0
    if payload.get("price_above_sma20"):
        score += 1
    if payload.get("sma20_above_sma50"):
        score += 1
    if (payload.get("momentum_10d_pct") or 0) > 0:
        score += 1
    if (payload.get("change_21d_pct") or 0) > 0:
        score += 1
    if (payload.get("macd_histogram") or 0) > 0:
        score += 0.5
    forecast_usable = payload.get("forecast_method") == "timegpt-1" or (
        (payload.get("forecast_trend_r2") or 0) >= 0.25
    )
    if forecast_usable:
        score += 0.5 if (payload.get("forecast_change_5d_pct") or 0) > 0 else -0.5
    rsi_value = payload.get("rsi_14")
    if rsi_value is not None and rsi_value > 70:
        score -= 0.5
    if rsi_value is not None and rsi_value < 30:
        score += 0.5
    signal = "bullish" if score >= 2.5 else "bearish" if score <= 0.5 else "neutral"
    confidence = min(0.55 + 0.08 * abs(score - 1.5), 0.85)
    return {
        "ticker": ticker,
        "signal": signal,
        "confidence": round(confidence, 2),
        "summary": (
            f"[mock] Price {payload.get('price')} vs SMA20 {payload.get('sma_20')} / "
            f"SMA50 {payload.get('sma_50')}, RSI {rsi_value}, 10d momentum "
            f"{payload.get('momentum_10d_pct')}%, 5d forecast "
            f"{payload.get('forecast_change_5d_pct')}% (trend R2 "
            f"{payload.get('forecast_trend_r2')}, {payload.get('forecast_method')}) "
            f"-> {signal}."
        ),
    }
