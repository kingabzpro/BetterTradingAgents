"""Fundamental Analyst: reads normalized company fundamentals."""

import json

from app.agents import clamp_conf, pick_signal, clip
from app.models import AgentResult, AnalystResult

NAME = "fundamental"
DISPLAY = "Fundamental Analyst"


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Fundamental Analyst",
        goal="Assess the business quality and valuation of a stock from basic fundamentals.",
        backstory=(
            "You are a bottom-up equity analyst. You care about revenue and earnings "
            "growth, margins, profitability and valuation multiples, and you keep it "
            "simple - no deep financial modeling."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict):
    from crewai import Task

    return Task(
        description=f"""Assess the fundamentals of stock ticker {ticker}.

Normalized fundamentals (percentages are plain numbers, e.g. 31.4 means 31.4%; keys may be missing):
{json.dumps(payload, indent=2)}

Consider growth (revenue_growth_ttm_pct, earnings_growth_ttm_pct), profitability (gross_margin_pct, net_margin_pct, roe_pct, eps_ttm) and valuation (pe_ratio_ttm, pe_ratio_forward, price_to_book). High growth with extreme valuation is a mixed picture, not automatically bullish.

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
    if (payload.get("revenue_growth_ttm_pct") or 0) > 10:
        score += 1
    if (payload.get("earnings_growth_ttm_pct") or 0) > 10:
        score += 1
    if (payload.get("net_margin_pct") or 0) > 15:
        score += 1
    pe = payload.get("pe_ratio_ttm")
    if pe is not None:
        if pe < 20:
            score += 1
        elif pe > 50:
            score -= 1
    signal = "bullish" if score >= 2.5 else "bearish" if score <= 0.5 else "neutral"
    return {
        "ticker": ticker,
        "signal": signal,
        "confidence": round(min(0.55 + 0.08 * abs(score - 1.5), 0.85), 2),
        "summary": (
            f"[mock] Revenue growth {payload.get('revenue_growth_ttm_pct')}%, "
            f"earnings growth {payload.get('earnings_growth_ttm_pct')}%, "
            f"net margin {payload.get('net_margin_pct')}%, PE {pe} -> {signal}."
        ),
    }
