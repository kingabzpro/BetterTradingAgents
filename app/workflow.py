"""Analysis workflow: 3 parallel research agents -> bull+bear in parallel -> manager.

Every agent run is one single-agent CrewAI crew. One agent failing never kills
the run - its slot becomes None and downstream agents are told the input is
missing.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

from app import risk
from app.agents import bear, bull, fundamental, manager, news, technical
from app.config import settings
from app.models import AgentResult, PortfolioSummary, StockAnalysis
from app.tools.indicators import compute_indicators
from app.tools.market_data import get_stock_data

logger = logging.getLogger("analysis")

Emit = Callable[[str, dict], Awaitable[None]]

_llm: Any = None
_llm_initialized = False
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def get_llm():
    """Build the CrewAI LLM once (single model for every agent)."""
    global _llm, _llm_initialized
    if not _llm_initialized:
        _llm_initialized = True
        if settings.llm_configured:
            from crewai import LLM

            model = settings.llm_model
            if not model.startswith("openai/"):
                model = f"openai/{model}"
            extra = {}
            if settings.llm_reasoning_effort:
                extra["reasoning_effort"] = settings.llm_reasoning_effort
            _llm = LLM(
                model=model,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                temperature=settings.llm_temperature,
                timeout=settings.llm_timeout_seconds,
                **extra,
            )
    return _llm


def extract_json(text: str) -> dict:
    """Parse a JSON object out of an LLM response (handles <think> blocks and fences)."""
    cleaned = _THINK_BLOCK.sub("", text).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue
    raise ValueError(f"no JSON object in model output: {text[:200]!r}")


async def _kick_once(agent, task) -> dict:
    from crewai import Crew

    crew = Crew(agents=[agent], tasks=[task])
    output = await asyncio.wait_for(
        crew.kickoff_async(), timeout=settings.llm_timeout_seconds
    )
    # Prefer CrewAI's pydantic conversion when it produced real content.
    pydantic = getattr(output, "pydantic", None)
    if pydantic is not None and getattr(pydantic, "summary", ""):
        return pydantic.model_dump()
    return extract_json(str(getattr(output, "raw", "")))


async def _run_agent(
    mod, ticker: str, emit: Emit, name: str | None = None, **task_payload
) -> dict | None:
    """Run one agent (LLM or mock), emitting started/completed/failed events."""
    agent_name = name or mod.NAME
    await emit("agent_started", {"ticker": ticker, "agent": agent_name})
    started = time.perf_counter()
    try:
        llm = get_llm()
        if llm is None:
            await asyncio.sleep(0.4)  # mock mode: give the UI a moment
            data = mod.mock(ticker, **task_payload)
        else:
            data = None
            last_error: Exception | None = None
            for _ in range(2):  # one retry on transient LLM failures
                try:
                    agent = mod.build_agent(llm)
                    task = mod.build_task(agent, ticker, **task_payload)
                    data = await _kick_once(agent, task)
                    break
                except Exception as exc:  # noqa: BLE001 - retry any agent failure
                    last_error = exc
            if data is None:
                raise last_error or RuntimeError("agent failed")
        duration = time.perf_counter() - started
        logger.info("[%s] completed %.1fs", agent_name, duration)
        await emit(
            "agent_completed",
            {
                "ticker": ticker,
                "agent": agent_name,
                "duration_s": round(duration, 1),
                "signal": data.get("signal") or data.get("decision"),
                "confidence": data.get("confidence") or data.get("score"),
                "summary": str(data.get("summary", ""))[:200],
            },
        )
        return data
    except Exception as exc:  # noqa: BLE001 - one agent failing must not kill the run
        logger.warning("[%s] failed: %s", agent_name, exc)
        await emit(
            "agent_failed",
            {"ticker": ticker, "agent": agent_name, "error": str(exc)[:200]},
        )
        return None


async def _portfolio_summary() -> PortfolioSummary | None:
    """Fetch the portfolio for the manager prompt and the risk gate; best-effort."""
    try:
        from app import portfolio

        return await portfolio.get_portfolio()
    except Exception as exc:  # noqa: BLE001 - portfolio context is best-effort
        logger.warning("[portfolio] summary failed: %s", exc)
        return None


def _portfolio_context(summary: PortfolioSummary | None) -> list[dict] | str:
    """Compact view of open positions for the manager prompt."""
    if summary is None:
        return "UNAVAILABLE - portfolio lookup failed"
    if not summary.positions:
        return "no open positions (flat)"
    return [
        {
            "ticker": p.ticker,
            "quantity": p.quantity,
            "entry_price": p.entry_price,
            "current_price": p.current_price,
            "unrealized_pnl_pct": p.pnl_pct,
            "weight_pct_of_equity": (
                round(p.value / summary.total_equity * 100, 1)
                if p.value is not None and summary.total_equity
                else None
            ),
        }
        for p in summary.positions
    ]


async def analyze_ticker(ticker: str, emit: Emit) -> StockAnalysis:
    """Full 3-stage workflow for one ticker."""
    started = time.perf_counter()
    await emit("ticker_started", {"ticker": ticker})
    logger.info("[analysis] Starting %s", ticker)

    try:
        market = await get_stock_data(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[analysis] %s aborted: market data failed: %s", ticker, exc)
        await emit("ticker_failed", {"ticker": ticker, "error": str(exc)[:200]})
        return StockAnalysis(
            ticker=ticker, error=f"market data failed: {exc}"[:300]
        )

    indicators = compute_indicators(
        market.closes, market.highs, market.lows, market.volumes
    )
    await emit(
        "ticker_data",
        {
            "ticker": ticker,
            "price": market.price,
            "company_name": market.company_name,
            "sources": market.sources,
        },
    )

    # Stage 1: three researchers in parallel.
    tech_data, fund_data, news_data = await asyncio.gather(
        _run_agent(technical, ticker, emit, payload=indicators),
        _run_agent(fundamental, ticker, emit, payload=market.fundamentals),
        _run_agent(news, ticker, emit, payload={"items": market.news}),
    )
    tech = technical.to_result(tech_data, ticker) if tech_data else None
    fund = fundamental.to_result(fund_data, ticker) if fund_data else None
    news_r = news.to_result(news_data, ticker) if news_data else None

    context = {
        "ticker": ticker,
        "price": market.price,
        "technical": tech.model_dump() if tech else "FAILED - unavailable",
        "fundamental": fund.model_dump() if fund else "FAILED - unavailable",
        "news": news_r.model_dump() if news_r else "FAILED - unavailable",
    }

    # Stage 2: bull and bear in parallel.
    bull_data, bear_data = await asyncio.gather(
        _run_agent(bull, ticker, emit, payload=context),
        _run_agent(bear, ticker, emit, payload=context),
    )

    # Stage 2b: rebuttal round - each side answers the other's argument
    # (skipped when either first-round brief failed).
    bull_rebuttal, bear_rebuttal = None, None
    if settings.debate_rounds >= 2:
        if bull_data and bear_data:
            bull_rebuttal, bear_rebuttal = await asyncio.gather(
                _run_agent(
                    bull,
                    ticker,
                    emit,
                    name="bull_rebuttal",
                    payload={
                        "research": context,
                        "own_round_1": bull_data,
                        "opponent_round_1": bear_data,
                    },
                    rebuttal=True,
                ),
                _run_agent(
                    bear,
                    ticker,
                    emit,
                    name="bear_rebuttal",
                    payload={
                        "research": context,
                        "own_round_1": bear_data,
                        "opponent_round_1": bull_data,
                    },
                    rebuttal=True,
                ),
            )
        else:
            for side in ("bull_rebuttal", "bear_rebuttal"):
                await emit(
                    "agent_failed",
                    {
                        "ticker": ticker,
                        "agent": side,
                        "error": "skipped: first-round debate incomplete",
                    },
                )

    bull_final = bull_rebuttal or bull_data
    bear_final = bear_rebuttal or bear_data
    bull_r = bull.to_result(bull_final, ticker) if bull_final else None
    bear_r = bear.to_result(bear_final, ticker) if bear_final else None

    # Stage 3: portfolio manager (sees existing holdings so decisions account
    # for exposure already taken; the debate transcript shows how the final
    # positions were reached).
    portfolio_summ = await _portfolio_summary()
    full_context = {
        **context,
        "bull": bull_r.model_dump() if bull_r else "FAILED - unavailable",
        "bear": bear_r.model_dump() if bear_r else "FAILED - unavailable",
        "debate": {
            "rounds": settings.debate_rounds,
            "bull_round_1": bull_data,
            "bear_round_1": bear_data,
            "bull_rebuttal": bull_rebuttal,
            "bear_rebuttal": bear_rebuttal,
        },
        "current_portfolio": _portfolio_context(portfolio_summ),
    }
    mgr_data = await _run_agent(manager, ticker, emit, payload=full_context)

    if mgr_data:
        final = manager.to_manager_result(mgr_data, ticker)
        decision, confidence = final.decision, final.confidence
        summary, bull_case, bear_case = (
            final.summary,
            final.bull_case,
            final.bear_case,
        )
        logger.info("[manager] %s %s %.0f%%", ticker, decision, confidence * 100)
    else:
        decision, confidence = "HOLD", 0.0
        summary = "Portfolio manager failed - defaulting to HOLD with no conviction."
        bull_case = bull_r.summary if bull_r else ""
        bear_case = bear_r.summary if bear_r else ""

    # Risk gate: deterministic sizing and exposure caps (ROADMAP 1.2).
    size_usd, risk_flags = None, []
    if mgr_data:
        decision, confidence, size_usd, risk_flags = risk.evaluate(
            decision,
            confidence,
            ticker,
            analysts=(tech, fund, news_r),
            vol_ann_pct=indicators.get("volatility_annualized_pct"),
            portfolio=portfolio_summ,
        )
    else:
        risk_flags = ["portfolio manager failed - defaulted to HOLD"]

    analysis = StockAnalysis(
        ticker=ticker,
        company_name=market.company_name,
        price=market.price,
        decision=decision,
        confidence=confidence,
        summary=summary,
        bull_case=bull_case,
        bear_case=bear_case,
        technical=tech,
        fundamental=fund,
        news=news_r,
        bull=bull_r,
        bear=bear_r,
        duration_s=round(time.perf_counter() - started, 1),
        suggested_size_usd=size_usd,
        risk_flags=risk_flags,
    )
    logger.info(
        "[analysis] %s completed in %.1fs (%s)", ticker, analysis.duration_s, decision
    )
    await emit(
        "ticker_completed",
        {
            "ticker": ticker,
            "decision": decision,
            "confidence": confidence,
            "duration_s": analysis.duration_s,
            "analysis": analysis.model_dump(),
        },
    )
    return analysis
