"""News Analyst: reads recent headlines (Finnhub -> Olostep -> yfinance)."""

import json

from app.agents import clamp_conf, pick_signal, clip
from app.models import AgentResult, AnalystResult

NAME = "news"
DISPLAY = "News Analyst"


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="News Analyst",
        goal="Judge whether recent company news is positive, negative or neutral overall.",
        backstory=(
            "You are a news analyst who reads only a handful of recent, relevant "
            "articles per company and focuses on what would actually move the stock: "
            "earnings surprises, guidance, product news, regulation, analyst actions."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict):
    from crewai import Task

    return Task(
        description=f"""Analyze recent news for stock ticker {ticker}.

Recent articles (newest first; 'content' when present is scraped article text):
{json.dumps(payload, indent=2, default=str)}

Focus on the few most market-moving items. Ignore stale or generic filler. If there is no meaningful news, say so and return neutral.

Respond with ONLY a JSON object, no markdown fences, no text outside the JSON:
{{"ticker": "{ticker}", "signal": "positive" | "negative" | "neutral", "confidence": <number 0.0-1.0>, "summary": "<at most 2 sentences citing the key stories>"}}""",
        expected_output=(
            "A JSON object with keys: ticker, signal (positive|negative|neutral), "
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


_POSITIVE_WORDS = ("beat", "beats", "surge", "soar", "record", "upgrade", "strong", "growth", "rally", "bullish")
_NEGATIVE_WORDS = ("miss", "misses", "plunge", "drop", "downgrade", "weak", "lawsuit", "probe", "recall", "bearish", "fall", "fears")


def mock(ticker: str, payload: dict) -> dict:
    """Keyword-scan fallback used when no LLM is configured."""
    items = payload.get("items") or []
    positive = negative = 0
    for item in items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        positive += sum(word in text for word in _POSITIVE_WORDS)
        negative += sum(word in text for word in _NEGATIVE_WORDS)
    if positive > negative * 1.5:
        signal = "positive"
    elif negative > positive * 1.5:
        signal = "negative"
    else:
        signal = "neutral"
    return {
        "ticker": ticker,
        "signal": signal,
        "confidence": 0.6,
        "summary": (
            f"[mock] Scanned {len(items)} recent headlines: {positive} positive vs "
            f"{negative} negative keyword hits -> {signal}."
        ),
    }
