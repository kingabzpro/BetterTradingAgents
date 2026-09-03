"""Bear Agent: the strongest reasonable case against the stock."""

import json

from app.agents import clamp_conf, clip
from app.models import AgentResult, DebateResult

NAME = "bear"
DISPLAY = "Bear Researcher"


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Bear Researcher",
        goal="Identify the most serious risks of holding or buying the stock right now.",
        backstory=(
            "You are the bear in a research debate. You hunt for valuation risk, "
            "deteriorating technicals, business risk, negative news and uncertainty. "
            "You argue only from the evidence given and never invent data."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict, rebuttal: bool = False):
    from crewai import Task

    if rebuttal:
        return Task(
            description=f"""Round 2 of the research debate for ticker {ticker}: answer the bull.

Original research the debate is based on:
{json.dumps(payload["research"], indent=2, default=str)}

Your round-1 bear argument: {json.dumps(payload["own_round_1"], indent=2, default=str)}
The bull's round-1 argument you must answer: {json.dumps(payload["opponent_round_1"], indent=2, default=str)}

Directly rebut the bull's strongest point, concede briefly where a bull point genuinely stands, then restate how serious the risks remain after the exchange.

Respond with ONLY a JSON object, no markdown fences, no text outside the JSON:
{{"score": <number 0.0-1.0 = bear case strength AFTER the rebuttal>, "summary": "<at most 3 sentences: rebuttal and final position>"}}""",
            expected_output=(
                "A JSON object with keys: score (0.0-1.0), summary (max 3 sentences)."
            ),
            agent=agent,
            output_pydantic=DebateResult,
        )

    return Task(
        description=f"""Build the bear case for ticker {ticker}.

Research results from the analysts (a value of null or "FAILED" means that input is unavailable - do not use it):
{json.dumps(payload, indent=2, default=str)}

Identify the most serious risks: valuation, negative technical signals, business risk, negative news, an unfavorable 5-day projection, uncertainty. Keep it short and concrete.

Respond with ONLY a JSON object, no markdown fences, no text outside the JSON:
{{"score": <number 0.0-1.0 = how serious the risks are>, "summary": "<at most 3 sentences>"}}""",
        expected_output="A JSON object with keys: score (0.0-1.0), summary (max 3 sentences).",
        agent=agent,
        output_pydantic=DebateResult,
    )


def to_result(data: dict, ticker: str) -> AgentResult:
    return AgentResult(
        agent=NAME,
        signal="bearish",
        confidence=clamp_conf(data.get("score")),
        summary=clip(data.get("summary", ""), 500),
    )


def mock(ticker: str, payload: dict, rebuttal: bool = False) -> dict:
    if rebuttal:
        own = payload["own_round_1"].get("score", 0.5)
        opponent = payload["opponent_round_1"].get("score", 0.5)
        score = round(min(1.0, max(0.0, 0.75 * own + 0.25 * (1 - opponent))), 2)
        return {
            "score": score,
            "summary": (
                f"[mock] Rebuttal: bull case at {opponent:.2f} acknowledged where it "
                f"stands; bear case restated at {score:.2f}."
            ),
        }
    score = 0.45
    for key in ("technical", "fundamental", "news", "forecast"):
        entry = payload.get(key)
        if isinstance(entry, dict):
            if entry.get("signal") in ("bearish", "negative"):
                score += 0.12
            elif entry.get("confidence", 0.5) < 0.45:
                score += 0.05  # uncertain evidence is itself a risk
    return {
        "score": round(min(score, 0.9), 2),
        "summary": "[mock] Bear case flags valuation and any bearish research inputs.",
    }
