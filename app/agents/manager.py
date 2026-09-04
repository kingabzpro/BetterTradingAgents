"""Portfolio Manager: final BUY / HOLD / SELL decision."""

import json

from app.agents import clamp_conf, clip
from app.models import AgentResult, ManagerResult

NAME = "manager"
DISPLAY = "Portfolio Manager"


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Portfolio Manager",
        goal="Weigh all research and the bull/bear debate into one clear decision.",
        backstory=(
            "You are a disciplined portfolio manager. You weigh evidence, not "
            "narratives. You act on the weight of evidence and decide HOLD only "
            "when the bull and bear cases genuinely balance or key inputs are "
            "missing. You only decide BUY, HOLD or SELL - nothing else."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, payload: dict):
    from crewai import Task

    return Task(
        description=f"""Make the final call for ticker {ticker}.

Full research dossier (values of null or "FAILED" mean that input is unavailable - explicitly account for missing information by being more conservative):
{json.dumps(payload, indent=2, default=str)}

Decision rules:
- "user_context" states the user's trading horizon (day_trade, short_term or long_term) with guidance on how to weigh evidence. Apply it: daily momentum matters far less for a long_term holder than for a day trader, and fundamentals matter less for a day trader.
- "forecast_assessment" standardizes the 5-day forecast against the stock's own noise band: |z| < 0.5 is statistical noise, |z| >= 1 is a real signal. Treat noise as neutral - it is not a reason to abstain. Treat a clearly bearish forecast (z <= -1) as a serious objection to BUY that needs a decisively stronger bull case to override, and a clearly bullish one (z >= 1) as a serious objection to SELL.
- BUY when the bull case outweighs the bear case on the available evidence. A moderate but consistent edge is enough - perfect certainty is rare and not required.
- SELL requires clear deterioration or dominant risk.
- Decide HOLD when the bull and bear cases genuinely balance or a key input is missing - not merely because the call feels close or the position has already moved.
- "current_portfolio" lists positions already held. Account for existing exposure: a BUY that adds to an already-large position, or a SELL when nothing is held, needs somewhat stronger justification.
- "past_decisions" is this system's own track record on this ticker (earlier calls with realized returns, alpha vs SPY and a one-line lesson); "cross_ticker_lessons" carries lessons from other tickers. Use them to repeat what worked and correct what did not - but one or two outcomes are weak evidence, never a substitute for the current research above.

Respond with ONLY a JSON object, no markdown fences, no text outside the JSON:
{{"ticker": "{ticker}", "decision": "BUY" | "HOLD" | "SELL", "confidence": <number 0.0-1.0>, "summary": "<at most 3 sentences explaining the decision>", "bull_case": "<at most 2 sentences>", "bear_case": "<at most 2 sentences>"}}""",
        expected_output=(
            "A JSON object with keys: ticker, decision (BUY|HOLD|SELL), confidence "
            "(0.0-1.0), summary, bull_case, bear_case."
        ),
        agent=agent,
        output_pydantic=ManagerResult,
    )


def to_result(data: dict, ticker: str) -> AgentResult:
    manager = to_manager_result(data, ticker)
    return AgentResult(
        agent=NAME,
        signal={"BUY": "bullish", "SELL": "bearish"}.get(manager.decision, "neutral"),
        confidence=manager.confidence,
        summary=manager.summary,
    )


def to_manager_result(data: dict, ticker: str) -> ManagerResult:
    decision = str(data.get("decision", "HOLD")).strip().upper()
    if "BUY" in decision:
        decision = "BUY"
    elif "SELL" in decision:
        decision = "SELL"
    else:
        decision = "HOLD"
    return ManagerResult(
        ticker=ticker,
        decision=decision,
        confidence=clamp_conf(data.get("confidence")),
        summary=clip(data.get("summary", ""), 600),
        bull_case=clip(data.get("bull_case", ""), 400),
        bear_case=clip(data.get("bear_case", ""), 400),
    )


def mock(ticker: str, payload: dict) -> dict:
    """Fallback: net score of the bull/bear debate decides."""
    bull = payload.get("bull") or {}
    bear = payload.get("bear") or {}
    bull_score = bull.get("confidence", 0.5) if isinstance(bull, dict) else 0.5
    bear_score = bear.get("confidence", 0.5) if isinstance(bear, dict) else 0.5
    net = bull_score - bear_score
    decision = "BUY" if net >= 0.15 else "SELL" if net <= -0.15 else "HOLD"
    return {
        "ticker": ticker,
        "decision": decision,
        "confidence": round(0.5 + abs(net), 2),
        "summary": f"[mock] Bull {bull_score:.2f} vs bear {bear_score:.2f} -> {decision}.",
        "bull_case": "[mock] See bull researcher summary.",
        "bear_case": "[mock] See bear researcher summary.",
    }
