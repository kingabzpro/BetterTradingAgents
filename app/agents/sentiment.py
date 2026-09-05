"""Sentiment Analyst: reads retail chatter from Reddit and StockTwits (Olostep).

Social sentiment predicts short-horizon returns for retail-driven names, but
only when the crowd is thick enough to mean anything - thin volume is flagged
and returned as low-confidence neutral instead of a signal (ROADMAP 3.1).
"""

import json

from app.agents import clamp_conf, pick_signal, clip
from app.agents.news import _NEGATIVE_WORDS, _POSITIVE_WORDS
from app.models import AgentResult, AnalystResult

NAME = "sentiment"
DISPLAY = "Sentiment Analyst"

# Below this many posts the crowd is too thin for the reading to mean anything.
MIN_ITEMS = 3


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Social Sentiment Analyst",
        goal="Judge whether retail investor chatter is bullish, bearish or neutral.",
        backstory=(
            "You are a sentiment analyst who reads what retail investors are "
            "saying about a stock on Reddit and StockTwits. You know social hype "
            "is noisy: bravado and pump talk are not the same as conviction, and "
            "a handful of posts is not a crowd."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict):
    from crewai import Task

    return Task(
        description=f"""Analyze social media sentiment for stock ticker {ticker}.

Recent social posts (Reddit / StockTwits, newest first):
{json.dumps(payload, indent=2, default=str)}

Weigh what the crowd actually says. Treat hype and pump-and-dump bravado skeptically, and discount repeated posts from the same thread. If there are fewer than {MIN_ITEMS} posts, the sample is too thin to mean anything: return neutral with low confidence and say so.

Respond with ONLY a JSON object, no markdown fences, no text outside the JSON:
{{"ticker": "{ticker}", "signal": "positive" | "negative" | "neutral", "confidence": <number 0.0-1.0>, "summary": "<at most 2 sentences describing the mood of the crowd">}}""",
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


# The news word lists plus social-specific slang.
_SOCIAL_POSITIVE = _POSITIVE_WORDS + (
    "moon",
    "moonish",
    "rocket",
    "pumping",
    "squeeze",
    "yolo",
    "undervalued",
    "loading",
    "calls",
    "gains",
)
_SOCIAL_NEGATIVE = _NEGATIVE_WORDS + (
    "dump",
    "bagholder",
    "bagholding",
    "tanking",
    "crash",
    "scam",
    "rug",
    "puts",
    "losses",
    "capitulation",
)


def mock(ticker: str, payload: dict) -> dict:
    """Keyword-scan fallback used when no LLM is configured."""
    items = payload.get("items") or []
    positive = negative = 0
    for item in items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        positive += sum(word in text for word in _SOCIAL_POSITIVE)
        negative += sum(word in text for word in _SOCIAL_NEGATIVE)
    if len(items) < MIN_ITEMS:
        return {
            "ticker": ticker,
            "signal": "neutral",
            "confidence": 0.35,
            "summary": (
                f"[mock] Social volume too thin to mean anything: {len(items)} post(s) "
                f"found (need {MIN_ITEMS}) -> neutral with low confidence."
            ),
        }
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
            f"[mock] Scanned {len(items)} social posts: {positive} positive vs "
            f"{negative} negative keyword hits -> {signal}."
        ),
    }
