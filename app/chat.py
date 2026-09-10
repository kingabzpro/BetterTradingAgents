"""Follow-up chat with the portfolio manager persona (user-led decisions).

The BUY/HOLD/SELL verdict on the results card is the system's synthesized
view; this module lets the user interrogate it. Each ticker of a finished
run gets a grounded Q&A built from that run's dossier plus the current
portfolio, so questions like "should I invest in this apart from my
portfolio?" get answers tied to the research instead of generic advice.
"""

import asyncio
import html
import json
import logging
import re
import textwrap

from app.config import settings
from app.models import PortfolioSummary, StockAnalysis
from app.workflow import (
    _BACKOFF_SECONDS,
    _classify_failure,
    fetch_portfolio_summary,
    get_chat_llm,
)

logger = logging.getLogger("analysis")

MAX_ANSWER_CHARS = 400
_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE = re.compile(r"\\?<\\?/think\s*>", re.IGNORECASE)
_THINK_OPEN = re.compile(r"\\?<think\b[^>]*>", re.IGNORECASE)


def build_agent(llm):
    from crewai import Agent

    return Agent(
        role="Portfolio Manager",
        goal="Give the user a brief, natural, useful answer to their latest message.",
        backstory=(
            "You are a practical portfolio manager having a normal conversation. "
            "Answer what the user just asked without narrating your research process. "
            "Use the supplied context silently and treat the user's statements about "
            "their own portfolio as current. This is an educational simulation, not "
            "investment advice."
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
        description=f"""Reply to this latest user message:
{question}

Supporting research for {ticker} (use silently; null and "FAILED" mean unavailable):
{json.dumps(dossier, indent=2, default=str)}

Earlier conversation, only if needed for context:
{transcript}

Rules:
- Answer the latest message first and sound like a helpful person, not a report.
- Trust what the user says about their holdings; do not correct it from an incomplete portfolio snapshot.
- Use relevant research facts, but never mention "the dossier", "my records", models, data coverage, or the research process unless asked.
- Give practical guidance for broad questions instead of listing caveats.
- Use no more than 2 short sentences (about 50 words).
- Output only the answer: no analysis, reasoning, transcript, markdown headings, or JSON.""",
        expected_output="A direct, natural answer of no more than two short sentences.",
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
            "manager_decision": analysis.manager_decision,
            "manager_confidence": analysis.manager_confidence,
            "final_decision": analysis.decision,
            "confidence": analysis.confidence,
            "summary": analysis.summary,
            "bull_case": analysis.bull_case,
            "bear_case": analysis.bear_case,
            "would_upgrade_if": analysis.would_upgrade_if or None,
            "would_downgrade_if": analysis.would_downgrade_if or None,
            "suggested_size_usd": analysis.suggested_size_usd,
            "risk_flags": analysis.risk_flags,
            "note": (
                "manager_decision is the manager's call before the deterministic "
                "risk gate; final_decision is what the risk gate returned."
            ),
        },
        "data_quality": analysis.data_quality.model_dump(),
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
    answer = clean_answer(str(getattr(output, "raw", "")))
    if not answer:
        raise ValueError("empty chat response")
    return answer


def clean_answer(raw: str) -> str:
    """Remove provider reasoning wrappers and keep chat replies brief."""
    cleaned = _THINK_BLOCK.sub("", html.unescape(raw))
    closers = list(_THINK_CLOSE.finditer(cleaned))
    if closers:
        cleaned = cleaned[closers[-1].end() :]
    cleaned = _THINK_OPEN.split(cleaned, maxsplit=1)[0].strip()
    return textwrap.shorten(cleaned, width=MAX_ANSWER_CHARS, placeholder="…")


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
