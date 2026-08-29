"""Bull Agent: the strongest reasonable case for buying."""

import json

from app.agents import clamp_conf, clip
from app.models import AgentResult, DebateResult

NAME = "bull"
DISPLAY = "Bull Researcher"


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Bull Researcher",
        goal="Build the strongest reasonable case for buying the stock right now.",
        backstory=(
            "You are the bull in a research debate. You are persuasive but honest - "
            "you only argue from the evidence given, never invent data, and you "
            "acknowledge that your job is one side of the argument."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict):
    from crewai import Task

    return Task(
        description=f"""Build the bull case for ticker {ticker}.

Research results from three analysts (a value of null or "FAILED" means that input is unavailable - do not use it):
{json.dumps(payload, indent=2, default=str)}

Argue the strongest reasonable BUY case from the available evidence: trend, growth, margins, positive news. Keep it short and concrete.

Respond with ONLY a JSON object, no markdown fences, no text outside the JSON:
{{"score": <number 0.0-1.0 = how strong the bull case is>, "summary": "<at most 3 sentences>"}}""",
        expected_output="A JSON object with keys: score (0.0-1.0), summary (max 3 sentences).",
        agent=agent,
        output_pydantic=DebateResult,
    )


def to_result(data: dict, ticker: str) -> AgentResult:
    return AgentResult(
        agent=NAME,
        signal="bullish",
        confidence=clamp_conf(data.get("score")),
        summary=clip(data.get("summary", ""), 500),
    )


def mock(ticker: str, payload: dict) -> dict:
    """Fallback: strongest bullish input wins."""
    best = 0.5
    for key in ("technical", "fundamental", "news"):
        entry = payload.get(key)
        if isinstance(entry, dict):
            best = max(best, entry.get("confidence", 0.5))
    return {
        "score": round(min(best + 0.05, 0.9), 2),
        "summary": "[mock] Bull case mirrors the most confident bullish research input.",
    }
