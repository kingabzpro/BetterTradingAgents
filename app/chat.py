"""Follow-up chat with the portfolio manager persona (user-led decisions).

The BUY/HOLD/SELL verdict on the results card is the system's synthesized
view; this module lets the user interrogate it. Each ticker of a finished
run gets a grounded Q&A built from that run's dossier plus the current
portfolio, so questions like "should I invest in this apart from my
portfolio?" get answers tied to the research instead of generic advice.
"""

import asyncio
import json
import logging
import re

from app.config import settings
from app.models import PortfolioSummary, StockAnalysis
from app.workflow import (
    _BACKOFF_SECONDS,
    _classify_failure,
    fetch_portfolio_summary,
    get_chat_llm,
)

logger = logging.getLogger("analysis")

MAX_ANSWER_CHARS = 4000
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Portfolio Manager",
        goal="Help the user reach their own decision with the research at hand.",
        backstory=(
            "You are a candid portfolio manager in a follow-up conversation "
            "with the user who just received your analysis. You answer in plain "
            "prose, ground every claim in the dossier you are given, and say "
            "plainly when the research does not cover something. The user makes "
            "the final call - you inform it, you do not push it. This is an "
            "educational simulation, never investment advice."
        ),
        llm=llm,
        allow_delegation=False,
    )


def build_task(agent, ticker: str, dossier: dict, history: list[dict], question: str):
    from crewai import Task

    transcript = "\n".join(
        f"{'User' if message['role'] == 'user' else 'Manager'}: "
        f"{message['content']}"
        for message in history
    ) or "(no earlier turns)"
    return Task(
        description=f"""The user just received the analysis below for ticker {ticker} and is asking follow-up questions.

Analysis dossier from the finished run (values of null or "FAILED" mean that input is unavailable - say so when it matters):
{json.dumps(dossier, indent=2, default=str)}

Conversation so far:
{transcript}

Answer the user's latest question:
{question}

Rules:
- Ground every claim in the dossier; quote the numbers in it when they matter.
- If the question reaches beyond this research (other tickers, personal finances, taxes), reason from the dossier where possible and clearly mark what it cannot cover.
- The user decides whether to invest - inform that decision instead of restating the BUY/HOLD/SELL verdict.
- Plain prose, at most around 150 words, no markdown headings, no JSON.""",
        expected_output="A short plain-text answer grounded in the dossier.",
        agent=agent,
    )


def _agent_view(result) -> dict | None:
    if result is None:
        return None
    return {
        "signal": result.signal,
        "confidence": result.confidence,
        "summary": result.summary,
    }


def _dossier(analysis: StockAnalysis, portfolio: PortfolioSummary | None) -> dict:
    """Compact, chat-sized view of one ticker's finished analysis."""
    if portfolio is None:
        holdings = "UNAVAILABLE - portfolio lookup failed"
    elif not portfolio.positions:
        holdings = "no open positions (flat)"
    else:
        holdings = [
            {
                "ticker": p.ticker,
                "quantity": p.quantity,
                "entry_price": p.entry_price,
                "current_price": p.current_price,
                "unrealized_pnl_pct": p.pnl_pct,
            }
            for p in portfolio.positions
        ]
    return {
        "ticker": analysis.ticker,
        "company_name": analysis.company_name,
        "price": analysis.price,
        "as_of": analysis.as_of,
        "system_view": {
            "decision": analysis.decision,
            "confidence": analysis.confidence,
            "summary": analysis.summary,
            "bull_case": analysis.bull_case,
            "bear_case": analysis.bear_case,
            "suggested_size_usd": analysis.suggested_size_usd,
            "risk_flags": analysis.risk_flags,
        },
        "analysts": {
            key: _agent_view(getattr(analysis, key))
            for key in ("technical", "fundamental", "news", "sentiment", "forecast")
        },
        "debate": {
            "bull": _agent_view(analysis.bull),
            "bear": _agent_view(analysis.bear),
            "bull_rebuttal": _agent_view(analysis.bull_rebuttal),
            "bear_rebuttal": _agent_view(analysis.bear_rebuttal),
        },
        "forecast_5d": {
            "price": analysis.forecast_price_5d,
            "change_pct": analysis.forecast_change_5d_pct,
            "noise_band_1sigma_pct": analysis.forecast_band_pct,
            "z": analysis.forecast_z,
            "method": analysis.forecast_method or None,
        },
        "current_portfolio": holdings,
        "past_calls": analysis.past_decisions or "none recorded",
    }


async def _ask_once(agent, task) -> str:
    from crewai import Crew

    crew = Crew(agents=[agent], tasks=[task])
    output = await asyncio.wait_for(
        crew.kickoff_async(), timeout=settings.llm_timeout_seconds
    )
    raw = _THINK_BLOCK.sub("", str(getattr(output, "raw", ""))).strip()
    if not raw:
        raise ValueError("empty chat response")
    return raw[:MAX_ANSWER_CHARS]


def mock_answer(analysis: StockAnalysis) -> str:
    """Fallback when no LLM is configured: mirror the call, point at evidence."""
    return (
        f"[mock] The finished run called {analysis.decision} on {analysis.ticker} "
        f"at {analysis.confidence:.0%} confidence: {analysis.summary} "
        "Configure LLM_API_KEY for full conversational answers; meanwhile the "
        "evidence behind this call is in the bull case, bear case and risk flags."
    )


async def answer_question(analysis: StockAnalysis, messages: list[dict]) -> str:
    """Answer the user's latest question (messages[-1]) about a finished run.

    Earlier turns give the conversation context; the portfolio is fetched
    best-effort so answers account for exposure the user already holds.
    """
    dossier = _dossier(analysis, await fetch_portfolio_summary())
    llm = get_chat_llm()
    if llm is None:
        return mock_answer(analysis)
    question = messages[-1]["content"]
    history = messages[:-1]
    agent = build_agent(llm)
    task = build_task(agent, analysis.ticker, dossier, history, question)
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            return await _ask_once(agent, task)
        except Exception as exc:  # noqa: BLE001 - classify, maybe retry once
            last_error = exc
            if _classify_failure(exc) in ("rate_limit", "server") and attempt < 2:
                await asyncio.sleep(_BACKOFF_SECONDS[min(attempt - 1, 1)])
                continue
            break
    logger.warning("[chat] %s failed: %s", analysis.ticker, last_error)
    raise RuntimeError(str(last_error)[:200])
