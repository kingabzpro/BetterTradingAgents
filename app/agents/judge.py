"""Judge Agent: neutral adjudicator of the bull/bear debate.

Replaces the old rebuttal round: instead of two self-scored rebuttals that
mostly restated round-1 confidence, one judge call cross-examines both cases
against the research and returns calibrated strengths plus a verdict.
"""

import json

from app.agents import clamp_conf, clip, pick_signal
from app.models import JudgeResult

NAME = "judge"
DISPLAY = "Debate Judge"


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Debate Judge",
        goal="Adjudicate the bull/bear debate into calibrated case strengths.",
        backstory=(
            "You are the neutral judge of a research debate, not a participant. "
            "You score each side by how well its claims survive the evidence, "
            "never by how confident it sounded, and you call out the point each "
            "side still has to answer."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict):
    from crewai import Task

    return Task(
        description=f"""Adjudicate the research debate for ticker {ticker}.

Research the debate is based on (a value of null or "FAILED" means that input is unavailable - do not use it):
{json.dumps(payload["research"], indent=2, default=str)}

Bull's argument:
{json.dumps(payload["bull_round_1"], indent=2, default=str)}

Bear's argument:
{json.dumps(payload["bear_round_1"], indent=2, default=str)}

Cross-examine both cases against the research. Score each side 0.0-1.0 on how well its strongest claims are actually supported: drop points for claims the evidence does not back, credit concrete evidence, and ignore confidence the evidence does not justify. Then state which case survived scrutiny and the one point each side still must answer.

Respond with ONLY a JSON object, no markdown fences, no text outside the JSON:
{{"signal": <"bullish" if the bull case is stronger, "bearish" if the bear case is, "neutral" if they genuinely balance>, "confidence": <number 0.0-1.0 = strength of the winning case>, "bull_strength": <number 0.0-1.0 = how well the bull case is supported>, "bear_strength": <number 0.0-1.0 = how well the bear case is supported>, "summary": "<at most 3 sentences: which case held up, the key concession, and what would change the call>"}}""",
        expected_output=(
            "A JSON object with keys: signal (bullish/bearish/neutral), confidence "
            "(0.0-1.0), bull_strength (0.0-1.0), bear_strength (0.0-1.0), summary "
            "(max 3 sentences)."
        ),
        agent=agent,
        output_pydantic=JudgeResult,
    )


def to_result(data: dict, ticker: str) -> JudgeResult:
    bull_strength = clamp_conf(data.get("bull_strength"), default=0.5)
    bear_strength = clamp_conf(data.get("bear_strength"), default=0.5)
    return JudgeResult(
        agent=NAME,
        signal=pick_signal(data.get("signal"), default="neutral"),
        confidence=clamp_conf(
            data.get("confidence"), default=max(bull_strength, bear_strength)
        ),
        bull_strength=bull_strength,
        bear_strength=bear_strength,
        summary=clip(data.get("summary", ""), 500),
    )


def mock(ticker: str, payload: dict) -> dict:
    bull = clamp_conf(payload["bull_round_1"].get("score"), default=0.5)
    bear = clamp_conf(payload["bear_round_1"].get("score"), default=0.5)
    # Deterministic cross-examination: each side loses ground proportional to
    # the opponent's strength, so a lopsided debate keeps its spread.
    bull_strength = round(clamp_conf(bull - 0.25 * bear), 2)
    bear_strength = round(clamp_conf(bear - 0.25 * bull), 2)
    if bull_strength - bear_strength >= 0.1:
        signal, confidence = "bullish", bull_strength
    elif bear_strength - bull_strength >= 0.1:
        signal, confidence = "bearish", bear_strength
    else:
        signal, confidence = "neutral", max(bull_strength, bear_strength)
    return {
        "signal": signal,
        "confidence": confidence,
        "bull_strength": bull_strength,
        "bear_strength": bear_strength,
        "summary": (
            f"[mock] Judge: bull case held at {bull_strength:.2f} vs bear case "
            f"{bear_strength:.2f}; the {signal} reading survives cross-examination."
        ),
    }
