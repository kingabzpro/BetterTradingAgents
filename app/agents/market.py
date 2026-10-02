"""Market Analyst: reads the broader market regime the stock trades in.

Analyzes overall market conditions - S&P 500 and Nasdaq trends, volatility
(VIX), interest rates (10-year yield) - and determines whether the broader
market is bullish, bearish or neutral. Economic events are not fetched by a
dedicated source; the analyst notes that its read is data-driven.
"""

import json

from app.agents import clamp_conf, clip, pick_signal
from app.models import AgentResult, AnalystResult

NAME = "market"
DISPLAY = "Market Analyst"


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Market Analyst",
        goal="Judge the broader market regime from index, volatility and rate data.",
        backstory=(
            "You are a macro strategist. You read what the overall market is doing "
            "- index trends, volatility, rates - before anyone looks at a single "
            "stock, because even the best setup struggles against a hostile market. "
            "All numbers are calculated by Python; you interpret, never invent."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict):
    from crewai import Task

    return Task(
        description=f"""Assess the overall market regime relevant to trading stock {ticker}.

Market data (computed by Python from ~6 months of daily index closes; a null or missing section means that input is unavailable - do not invent it):
{json.dumps(payload, indent=2, default=str)}

Read the regime across four lenses: trend (is the S&P 500 above its 20-day average, are 5d/21d index changes positive), breadth of the move (S&P and Nasdaq agreeing is stronger than one index alone), volatility (VIX level: below 15 is calm, above 25 is stress; a rising VIX opposes long exposure), and rates (the 10-year yield level and its 5-day move: a sharp yield rise is a headwind, especially for growth stocks). Then decide whether the broader market is bullish, bearish or neutral for the next days to weeks. Note that this read is data-driven: it does not see an economic calendar.

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
    spx = payload.get("spx") or {}
    nasdaq = payload.get("nasdaq") or {}
    vix = payload.get("vix") or {}
    rates = payload.get("rates_10y") or {}
    score = 0.0
    if (spx.get("change_5d_pct") or 0) > 0:
        score += 1
    if (nasdaq.get("change_5d_pct") or 0) > 0:
        score += 1
    if spx.get("above_sma20"):
        score += 1
    vix_level = vix.get("last")
    if (vix.get("change_5d_pct") or 0) < 0:
        score += 0.5
    if vix_level is not None and vix_level > 25:
        score -= 1
    if (rates.get("change_5d_pct") or 0) > 2:
        score -= 0.5
    signal = "bullish" if score >= 2 else "bearish" if score <= -0.5 else "neutral"
    confidence = min(0.55 + 0.08 * abs(score - 1.0), 0.85)
    return {
        "ticker": ticker,
        "signal": signal,
        "confidence": round(confidence, 2),
        "summary": (
            f"[mock] S&P 5d {spx.get('change_5d_pct')}%, Nasdaq 5d "
            f"{nasdaq.get('change_5d_pct')}%, VIX {vix_level} "
            f"({vix.get('change_5d_pct')}% 5d), 10y yield {rates.get('last')}% "
            f"({rates.get('change_5d_pct')}% 5d) -> {signal}."
        ),
    }
