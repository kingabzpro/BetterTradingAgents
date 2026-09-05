"""Analysis workflow: 5 parallel research agents -> bull+bear in parallel -> manager.

Every agent run is one single-agent CrewAI crew. One agent failing never kills
the run - its slot becomes None and downstream agents are told the input is
missing. With STREAM_REASONING on, each agent's tokens stream to the UI as
agent_token events (ROADMAP 3.2); mock mode streams the deterministic text
word-by-word so the UI path is always testable.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

from app import risk
from app.agents import bear, bull, forecast, fundamental, manager, news, sentiment, technical
from app.config import settings
from app.depth import DEFAULT_DEPTH, depth_profile
from app.models import AgentResult, PortfolioSummary, SourceReference, StockAnalysis
from app.outlook import DEFAULT_OUTLOOK, user_context
from app.tools.indicators import compute_indicators, historical_forecast
from app.tools.market_data import (
    MarketData,
    get_stock_data,
    get_timegpt_forecast,
)

logger = logging.getLogger("analysis")

Emit = Callable[[str, dict], Awaitable[None]]

# Live reasoning stream (ROADMAP 3.2). The UI pane keeps a sliding window per
# agent; STREAM_MAX_CHARS guards the event volume of a runaway stream.
STREAM_WINDOW_CHARS = 2048
STREAM_MAX_CHARS = 16_384
_MOCK_STREAM_WORDS = 5  # mock mode: words per streamed chunk
_MOCK_STREAM_DELAY = 0.025  # seconds between chunks

# Which role each agent runs as - per-role LLM overrides (ROADMAP 2.3) key
# off this: cheap fast models for the researchers, a stronger one for the call.
ROLE_BY_AGENT = {
    "technical": "analysts",
    "fundamental": "analysts",
    "news": "analysts",
    "sentiment": "analysts",
    "forecast": "analysts",
    "bull": "debate",
    "bear": "debate",
    "manager": "manager",
}
ROLES = ("manager", "analysts", "debate")

_llms: dict[str, Any] = {}  # role -> CrewAI LLM (None in mock mode)
_llm_roles_initialized: set[str] = set()
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_BACKOFF_SECONDS = (1.0, 4.0)  # HTTP 429/5xx: wait, retry, wait longer


# Model prefixes crewai routes itself; anything else (bare names and org/model
# forms like zai-org/GLM-5.3) must go through the OpenAI-compatible client.
_KNOWN_PROVIDER_PREFIXES = (
    "openai/",
    "openrouter/",
    "deepseek/",
    "ollama/",
    "groq/",
    "mistral/",
    "anthropic/",
    "azure/",
    "gemini/",
    "bedrock/",
    "cerebras/",
    "dashscope/",
    "snowflake/",
)


def _route_model(model: str) -> str:
    if model.startswith(_KNOWN_PROVIDER_PREFIXES):
        return model
    return f"openai/{model}"


def _build_llm(role: str, json_mode: bool = True, stream: bool | None = None) -> Any:
    """Construct the CrewAI LLM for one role from settings.llm_for(role).

    json_mode asks the provider for guaranteed JSON output; reasoning_effort
    is also sent verbatim in the request body because the crewai field alone
    only reaches o1-style models on /chat/completions. stream=None follows
    settings.stream_reasoning (ROADMAP 3.2 live token events).
    """
    from crewai import LLM

    conf = settings.llm_for(role)
    extra: dict = {}
    if settings.llm_reasoning_effort:
        extra["reasoning_effort"] = settings.llm_reasoning_effort
    if json_mode:
        extra["response_format"] = {"type": "json_object"}
    if stream is None:
        stream = settings.stream_reasoning
    return LLM(
        model=_route_model(conf["model"]),
        base_url=conf["base_url"],
        api_key=conf["api_key"],
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
        reasoning_effort=settings.llm_reasoning_effort or None,
        stream=stream,
        additional_params=extra,
    )


def get_llm(role: str = "analysts") -> Any:
    """Build (once) and cache the CrewAI LLM for one role.

    Roles: manager / analysts / debate, resolved via settings.llm_for -
    per-role env overrides fall back to the global LLM_* values. Returns
    None when no LLM is configured (mock mode).
    """
    if role not in _llm_roles_initialized:
        _llm_roles_initialized.add(role)
        if settings.llm_configured:
            try:
                _llms[role] = _build_llm(role)
            except Exception as exc:  # noqa: BLE001 - degrade to mock, don't crash
                logger.error("[llm] could not build LLM for role %s: %s", role, exc)
                _llms[role] = None
        else:
            _llms[role] = None
    return _llms.get(role)


def _drop_json_mode(role: str) -> Any:
    """Rebuild a role's LLM without response_format after the provider
    rejected it (feature-detect on first use, not with a probe call)."""
    try:
        _llms[role] = _build_llm(role, json_mode=False)
        logger.warning(
            "[llm] JSON mode disabled for role %s - provider rejected it", role
        )
        return _llms[role]
    except Exception as exc:  # noqa: BLE001
        logger.error("[llm] could not rebuild LLM for role %s: %s", role, exc)
        return None


def _drop_stream_mode(role: str) -> Any:
    """Rebuild a role's LLM without streaming after the provider rejected it
    (feature-detect on first use, exactly like the JSON-mode fallback). The
    JSON-mode setting of the current LLM is preserved."""
    current = _llms.get(role)
    json_mode = current is None or "response_format" in (
        getattr(current, "additional_params", None) or {}
    )
    try:
        _llms[role] = _build_llm(role, json_mode=json_mode, stream=False)
        logger.warning("[llm] streaming disabled for role %s - provider rejected it", role)
        return _llms[role]
    except Exception as exc:  # noqa: BLE001
        logger.error("[llm] could not rebuild LLM for role %s: %s", role, exc)
        return None


def _classify_failure(exc: Exception) -> str:
    """Bucket an LLM failure to pick the retry strategy (ROADMAP 2.3)."""
    text = f"{type(exc).__name__} {exc}".lower()
    if "stream" in text:
        # Checked before response_format: combined rejections ("stream is not
        # supported with response_format") must drop streaming first.
        return "stream"
    if "response_format" in text:
        return "response_format"
    if (
        "429" in text
        or "rate limit" in text
        or "ratelimit" in text
        or "too many requests" in text
    ):
        return "rate_limit"
    if any(
        hint in text
        for hint in (
            "500",
            "502",
            "503",
            "504",
            "overloaded",
            "bad gateway",
            "service unavailable",
            "internal server",
            "timed out",
            "timeout",
        )
    ):
        return "server"
    if "json" in text or isinstance(exc, ValueError):
        return "bad_output"
    return "other"


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


def _usage_snapshot(output: Any) -> dict:
    """Token usage of one crew run, tolerating missing provider metrics."""
    usage = getattr(output, "token_usage", None)
    if usage is None:
        return {}
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    reasoning = int(getattr(usage, "reasoning_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or 0)
    total = total or (prompt + completion)
    if not (prompt or completion or total):
        return {}
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }


async def _kick_once(agent, task) -> tuple[dict, dict]:
    from crewai import Crew

    crew = Crew(agents=[agent], tasks=[task])
    output = await asyncio.wait_for(
        crew.kickoff_async(), timeout=settings.llm_timeout_seconds
    )
    usage = _usage_snapshot(output)
    # Prefer CrewAI's pydantic conversion when it produced real content.
    pydantic = getattr(output, "pydantic", None)
    if pydantic is not None and getattr(pydantic, "summary", ""):
        return pydantic.model_dump(), usage
    json_dict = getattr(output, "json_dict", None)
    if isinstance(json_dict, dict) and json_dict:
        return json_dict, usage
    return extract_json(str(getattr(output, "raw", ""))), usage


def _token_sink(ticker: str, agent_name: str, emit: Emit) -> Callable[[Any, Any], None]:
    """CrewAI stream sink that forwards one agent's tokens as agent_token events.

    Attached per agent run via crewai's scoped stream sinks, so concurrent
    agents never receive each other's tokens. Only content chunks are
    forwarded (thinking deltas stay private), a sliding window is kept for
    debugging, and STREAM_MAX_CHARS caps the total so a runaway stream cannot
    flood the event queues. Chunks can arrive on a worker thread (crewai runs
    parts of kickoff in executors, with the sink context copied along), so
    forwarding goes through the loop captured here + call_soon_threadsafe.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # defensive: _run_agent always runs inside a loop
        loop = None
    state = {"sent": 0, "window": ""}

    def _forward(payload: dict) -> None:
        loop.create_task(emit("agent_token", payload))

    def sink(source: Any, event: Any) -> None:  # noqa: ARG001 - crewai contract
        if loop is None or getattr(event, "type", "") != "llm_stream_chunk":
            return
        chunk = str(getattr(event, "chunk", "") or "")
        if not chunk:
            return
        state["window"] = (state["window"] + chunk)[-STREAM_WINDOW_CHARS:]
        if state["sent"] >= STREAM_MAX_CHARS:
            return
        state["sent"] += len(chunk)
        payload = {"ticker": ticker, "agent": agent_name, "text": chunk}
        if state["sent"] >= STREAM_MAX_CHARS:
            payload["truncated"] = True
        try:
            loop.call_soon_threadsafe(_forward, payload)
        except RuntimeError:  # loop already closed - the run is over anyway
            pass

    return sink


async def _stream_mock_text(ticker: str, agent_name: str, text: str, emit: Emit) -> None:
    """Mock mode: stream the deterministic summary word-by-word so the live
    reasoning UI path is always testable (ROADMAP 3.2)."""
    words = str(text or "").split()
    for i in range(0, len(words), _MOCK_STREAM_WORDS):
        batch = " ".join(words[i : i + _MOCK_STREAM_WORDS]) + " "
        await emit(
            "agent_token", {"ticker": ticker, "agent": agent_name, "text": batch}
        )
        await asyncio.sleep(_MOCK_STREAM_DELAY)


async def _run_agent(
    mod,
    ticker: str,
    emit: Emit,
    name: str | None = None,
    token_totals: dict | None = None,
    live: bool = True,
    **task_payload,
) -> dict | None:
    """Run one agent (LLM or mock), emitting started/completed/failed events.

    LLM failures get one smart retry each (ROADMAP 2.3): 429/5xx waits and
    retries (1s, then 4s), a rejected response_format disables JSON mode for
    the role, a rejected stream disables streaming for the role, and a
    malformed output is retried once with the validation error appended to
    the task. `token_totals` accumulates per-run usage; `live` gates the
    cosmetic token stream (backtest replays skip it).
    """
    agent_name = name or mod.NAME
    role = ROLE_BY_AGENT.get(mod.NAME, "analysts")
    await emit("agent_started", {"ticker": ticker, "agent": agent_name})
    started = time.perf_counter()
    try:
        llm = get_llm(role)
        if llm is None:
            await asyncio.sleep(0.4)  # mock mode: give the UI a moment
            data = mod.mock(ticker, **task_payload)
            usage: dict = {}
            if live and settings.stream_reasoning:
                await _stream_mock_text(
                    ticker, agent_name, str(data.get("summary", "")), emit
                )
        else:
            data = None
            usage = {}
            last_error: Exception | None = None
            correction: str | None = None
            for attempt in range(1, 4):
                try:
                    agent = mod.build_agent(llm)
                    task = mod.build_task(agent, ticker, **task_payload)
                    if correction is not None:
                        task.description = correction + task.description
                    if getattr(llm, "stream", False):
                        # Scoped per agent run: concurrent agents of the same
                        # role never see each other's tokens (ROADMAP 3.2).
                        from crewai.events.stream_context import (
                            add_stream_sink,
                            reset_stream_sinks,
                        )

                        sink = _token_sink(ticker, agent_name, emit)
                        sink_token = add_stream_sink(sink)
                        try:
                            data, usage = await _kick_once(agent, task)
                        finally:
                            reset_stream_sinks(sink_token)
                    else:
                        data, usage = await _kick_once(agent, task)
                    # Let token-forward tasks scheduled by the sink reach the
                    # queues before agent_completed closes the pane's stream.
                    await asyncio.sleep(0)
                    break
                except Exception as exc:  # noqa: BLE001 - classify, maybe retry
                    last_error = exc
                    kind = _classify_failure(exc)
                    detail = str(exc)[:120]
                    if kind == "stream":
                        replacement = _drop_stream_mode(role)
                        if replacement is None:
                            break
                        logger.warning(
                            "[%s] provider rejected streaming; retrying without it",
                            agent_name,
                        )
                        llm = replacement
                        continue
                    if kind == "response_format":
                        replacement = _drop_json_mode(role)
                        if replacement is None:
                            break
                        logger.warning(
                            "[%s] provider rejected JSON mode; retrying without it",
                            agent_name,
                        )
                        llm = replacement
                        continue
                    if kind in ("rate_limit", "server") and attempt < 3:
                        wait = _BACKOFF_SECONDS[min(attempt - 1, 1)]
                        logger.warning(
                            "[%s] %s error (%s); retry %d/2 in %.0fs",
                            agent_name,
                            kind,
                            detail,
                            attempt,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    if kind == "bad_output" and correction is None and attempt < 3:
                        logger.warning(
                            "[%s] malformed output (%s); retrying with the error fed back",
                            agent_name,
                            detail,
                        )
                        correction = (
                            f"Your previous response was rejected: {str(exc)[:300]} "
                            "Respond with ONLY a corrected JSON object matching the "
                            "requested schema - no markdown fences, no text outside "
                            "the JSON.\n\n"
                        )
                        continue
                    break  # unknown failure or retry budget spent
            if data is None:
                raise last_error or RuntimeError("agent failed")
        duration = time.perf_counter() - started
        logger.info("[%s] completed %.1fs", agent_name, duration)
        if token_totals is not None:
            for key, value in usage.items():
                token_totals[key] = token_totals.get(key, 0) + value
        await emit(
            "agent_completed",
            {
                "ticker": ticker,
                "agent": agent_name,
                "duration_s": round(duration, 1),
                "signal": data.get("signal") or data.get("decision"),
                "confidence": data.get("confidence") or data.get("score"),
                "summary": str(data.get("summary", ""))[:200],
                "tokens": usage.get("total_tokens", 0),
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


async def fetch_portfolio_summary() -> PortfolioSummary | None:
    """Fetch the portfolio for the manager prompt and the risk gate; best-effort.

    Multi-ticker runs fetch this once and share it (see app.runs) instead of
    re-fetching prices for every held position once per ticker.
    """
    try:
        from app import portfolio

        return await portfolio.get_portfolio()
    except Exception as exc:  # noqa: BLE001 - portfolio context is best-effort
        logger.warning("[portfolio] summary failed: %s", exc)
        return None


async def fetch_past_decisions(ticker: str) -> dict | None:
    """Graded past calls on this ticker + cross-ticker lessons; best-effort."""
    try:
        from app import memory

        return await memory.get_reflections(ticker)
    except Exception as exc:  # noqa: BLE001 - memory context is best-effort
        logger.warning("[memory] reflections failed: %s", exc)
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


def _source_references(market: Any) -> list[SourceReference]:
    """Reduce provider payloads to safe provenance metadata for the UI."""
    references: list[SourceReference] = []
    seen: set[tuple[str, str]] = set()
    fallback_provider = market.sources.get("news", "unknown")
    for item in market.news[:8]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        provider = str(item.get("source") or fallback_provider).strip() or "unknown"
        url = str(item.get("url") or "").strip()
        key = (title.casefold(), url)
        if key in seen:
            continue
        seen.add(key)
        references.append(
            SourceReference(
                kind="news",
                title=title[:240],
                provider=provider[:100],
                url=url,
                published_at=str(item.get("published") or "").strip() or None,
            )
        )
    # A few social posts so the sentiment reading can be checked by hand.
    social_provider = market.sources.get("social", "unknown")
    for item in market.social[:3]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        url = str(item.get("url") or "").strip()
        key = (title.casefold(), url)
        if key in seen:
            continue
        seen.add(key)
        references.append(
            SourceReference(
                kind="social",
                title=title[:240],
                provider=(str(item.get("source") or social_provider).strip() or "social")[:100],
                url=url,
            )
        )
    return references


async def analyze_ticker(
    ticker: str,
    emit: Emit,
    portfolio_summary: PortfolioSummary | None = None,
    outlook: str = DEFAULT_OUTLOOK,
    depth: str = DEFAULT_DEPTH,
    run_id: str = "",
    market_data: MarketData | None = None,
    live_context: bool = True,
) -> StockAnalysis:
    """Full 3-stage workflow for one ticker.

    `portfolio_summary` lets a multi-ticker run fetch the portfolio once and
    share it across tickers; when omitted it is fetched here. `outlook` is the
    user's chosen horizon (day_trade / short_term / long_term) and is injected
    into every agent payload so the whole crew weighs evidence for that horizon.
    `depth` (fast / medium / expert) selects which researchers run and whether
    the debate gets a rebuttal round - fewer agents, faster run. `run_id` tags
    the decision-memory row written at the end of a successful run.
    `market_data` injects a pre-built snapshot (backtests replay date T with
    only data known at T); `live_context=False` is the backtest mode: no
    portfolio fetch, no decision-memory lookup or recording - both would leak
    information from after the replayed date.
    """
    started = time.perf_counter()
    await emit("ticker_started", {"ticker": ticker})
    logger.info("[analysis] Starting %s", ticker)

    if market_data is not None:
        market = market_data
        if market.price is None:
            await emit("ticker_failed", {"ticker": ticker, "error": "no price in snapshot"})
            return StockAnalysis(ticker=ticker, error="no price in snapshot")
    else:
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
    prof = depth_profile(depth)
    research: tuple[str, ...] = prof["research"]
    local_forecast: dict | None = None
    timegpt_forecast: dict | None = None
    if "forecast" in research:
        local_forecast = historical_forecast(market.closes)
        timegpt_forecast = await get_timegpt_forecast(market.closes)
        if timegpt_forecast:
            indicators.update(timegpt_forecast)
            market.sources["forecast"] = "timegpt"
        else:
            market.sources["forecast"] = "local"
    else:
        # "none" is filtered out of the UI's provider line.
        market.sources["forecast"] = "none"
    await emit(
        "ticker_data",
        {
            "ticker": ticker,
            "price": market.price,
            "company_name": market.company_name,
            "sources": market.sources,
        },
    )

    # Stage 1: the profile's researchers in parallel. Excluded researchers
    # never run (no events either - the UI shows no row for them).
    token_totals: dict[str, int] = {}
    user_ctx = user_context(outlook)
    forecast_payload = {
        "price": market.price,
        "history_days": len(market.closes),
        "timegpt_forecast": timegpt_forecast,
        "local_trend_forecast": local_forecast,
        "user_context": user_ctx,
        "trend_context": {
            key: indicators.get(key)
            for key in (
                "sma_20",
                "sma_50",
                "change_5d_pct",
                "change_21d_pct",
                "rsi_14",
                "volatility_annualized_pct",
                "atr_pct_of_price",
            )
        },
    }

    async def run_research(key: str) -> dict | None:
        if key == "technical":
            return await _run_agent(
                technical,
                ticker,
                emit,
                token_totals=token_totals,
                live=live_context,
                payload={**indicators, "user_context": user_ctx},
            )
        if key == "fundamental":
            return await _run_agent(
                fundamental,
                ticker,
                emit,
                token_totals=token_totals,
                live=live_context,
                payload={**market.fundamentals, "user_context": user_ctx},
            )
        if key == "news":
            return await _run_agent(
                news,
                ticker,
                emit,
                token_totals=token_totals,
                live=live_context,
                payload={"items": market.news, "user_context": user_ctx},
            )
        if key == "sentiment":
            return await _run_agent(
                sentiment,
                ticker,
                emit,
                token_totals=token_totals,
                live=live_context,
                payload={"items": market.social, "user_context": user_ctx},
            )
        return await _run_agent(
            forecast,
            ticker,
            emit,
            token_totals=token_totals,
            live=live_context,
            payload=forecast_payload,
        )

    order = ("technical", "fundamental", "news", "forecast", "sentiment")
    research_keys = [key for key in order if key in research]
    research_data = dict(
        zip(
            research_keys,
            await asyncio.gather(*(run_research(key) for key in research_keys)),
        )
    )
    tech_data = research_data.get("technical")
    fund_data = research_data.get("fundamental")
    news_data = research_data.get("news")
    sentiment_data = research_data.get("sentiment")
    forecast_data = research_data.get("forecast")
    tech = technical.to_result(tech_data, ticker) if tech_data else None
    fund = fundamental.to_result(fund_data, ticker) if fund_data else None
    news_r = news.to_result(news_data, ticker) if news_data else None
    sentiment_r = sentiment.to_result(sentiment_data, ticker) if sentiment_data else None
    forecast_r = forecast.to_result(forecast_data, ticker) if forecast_data else None

    def slot(key: str, result: AgentResult | None) -> dict | str:
        if result is not None:
            return result.model_dump()
        if key in research:
            return "FAILED - researcher ran but returned nothing usable"
        return f"SKIPPED - not requested in the {prof['label']} depth profile"

    context = {
        "ticker": ticker,
        "price": market.price,
        "user_context": user_ctx,
        "technical": slot("technical", tech),
        "fundamental": slot("fundamental", fund),
        "news": slot("news", news_r),
        "sentiment": slot("sentiment", sentiment_r),
        "forecast": slot("forecast", forecast_r),
    }

    # Stage 2: bull and bear in parallel.
    bull_data, bear_data = await asyncio.gather(
        _run_agent(
            bull, ticker, emit, token_totals=token_totals, live=live_context,
            payload=context,
        ),
        _run_agent(
            bear, ticker, emit, token_totals=token_totals, live=live_context,
            payload=context,
        ),
    )

    # Stage 2b: rebuttal round - each side answers the other's argument.
    # Only expert depth asks for it (and the server must allow DEBATE_ROUNDS >= 2);
    # it is skipped entirely when either first-round brief failed.
    bull_rebuttal, bear_rebuttal = None, None
    if prof["rebuttals"] and settings.debate_rounds >= 2:
        if bull_data and bear_data:
            bull_rebuttal, bear_rebuttal = await asyncio.gather(
                _run_agent(
                    bull,
                    ticker,
                    emit,
                    name="bull_rebuttal",
                    token_totals=token_totals,
                    live=live_context,
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
                    token_totals=token_totals,
                    live=live_context,
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
    bull_rebuttal_r = bull.to_result(bull_rebuttal, ticker) if bull_rebuttal else None
    bear_rebuttal_r = bear.to_result(bear_rebuttal, ticker) if bear_rebuttal else None

    # Stage 3: portfolio manager (sees existing holdings so decisions account
    # for exposure already taken; the debate transcript shows how the final
    # positions were reached). Backtest replay skips live context entirely.
    if live_context:
        portfolio_summ = (
            portfolio_summary
            if portfolio_summary is not None
            else await fetch_portfolio_summary()
        )
        # Decision memory (ROADMAP 1.1): the system's own graded past calls act
        # as a track record in the dossier; best-effort exactly like the portfolio.
        past = await fetch_past_decisions(ticker)
    else:
        portfolio_summ = None
        past = {"past_decisions": [], "cross_ticker_lessons": []}
    past_decisions_ctx = (
        "UNAVAILABLE - decision memory lookup failed"
        if past is None
        else (past["past_decisions"] or "none yet - no recorded decisions for this ticker")
    )
    cross_lessons_ctx = (
        "UNAVAILABLE - decision memory lookup failed"
        if past is None
        else (past["cross_ticker_lessons"] or "none yet - no graded decisions on other tickers")
    )
    # Standardize the forecast against the stock's own 5-day noise band so the
    # manager and the risk gate read the same number (see app.risk).
    vol_ann_pct = indicators.get("volatility_annualized_pct")
    forecast_change_pct = indicators.get("forecast_change_5d_pct")
    forecast_band_pct = risk.forecast_noise_pct(vol_ann_pct)
    forecast_z_value = risk.forecast_z(forecast_change_pct, vol_ann_pct)
    forecast_assessment = {
        "forecast_change_5d_pct": forecast_change_pct,
        "noise_band_1sigma_pct": None
        if forecast_band_pct is None
        else round(forecast_band_pct, 2),
        "z": None if forecast_z_value is None else round(forecast_z_value, 2),
        "signal": risk.forecast_signal(forecast_z_value),
        "reading": (
            "unavailable - no forecast or no volatility"
            if forecast_z_value is None
            else f"the forecast is {abs(forecast_z_value):.2f} sigma "
            f"({'inside' if abs(forecast_z_value) < 0.5 else 'beyond'} the "
            f"half-sigma noise threshold)"
        ),
    }
    full_context = {
        **context,
        "forecast_assessment": forecast_assessment,
        "bull": bull_r.model_dump() if bull_r else "FAILED - unavailable",
        "bear": bear_r.model_dump() if bear_r else "FAILED - unavailable",
        "debate": {
            "rounds": 2 if (prof["rebuttals"] and settings.debate_rounds >= 2) else 1,
            "bull_round_1": bull_data,
            "bear_round_1": bear_data,
            "bull_rebuttal": bull_rebuttal,
            "bear_rebuttal": bear_rebuttal,
        },
        "current_portfolio": _portfolio_context(portfolio_summ),
        "past_decisions": past_decisions_ctx,
        "cross_ticker_lessons": cross_lessons_ctx,
    }
    mgr_data = await _run_agent(
        manager,
        ticker,
        emit,
        token_totals=token_totals,
        live=live_context,
        payload=full_context,
    )

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

    # Risk gate: deterministic sizing, forecast check, and exposure caps.
    size_usd, risk_flags = None, []
    if mgr_data:
        decision, confidence, size_usd, risk_flags = risk.evaluate(
            decision,
            confidence,
            ticker,
            analysts=(tech, fund, news_r, sentiment_r),
            vol_ann_pct=vol_ann_pct,
            portfolio=portfolio_summ,
            forecast_change_pct=forecast_change_pct,
        )
    else:
        risk_flags = ["portfolio manager failed - defaulted to HOLD"]

    analysis = StockAnalysis(
        ticker=ticker,
        company_name=market.company_name,
        price=market.price,
        forecast_price_5d=indicators.get("forecast_price_5d"),
        forecast_change_5d_pct=indicators.get("forecast_change_5d_pct"),
        forecast_trend_r2=indicators.get("forecast_trend_r2"),
        forecast_method=indicators.get("forecast_method", ""),
        forecast_band_pct=None if forecast_band_pct is None else round(forecast_band_pct, 2),
        forecast_z=None if forecast_z_value is None else round(forecast_z_value, 2),
        decision=decision,
        confidence=confidence,
        summary=summary,
        bull_case=bull_case,
        bear_case=bear_case,
        technical=tech,
        fundamental=fund,
        news=news_r,
        sentiment=sentiment_r,
        forecast=forecast_r,
        bull=bull_r,
        bear=bear_r,
        bull_rebuttal=bull_rebuttal_r,
        bear_rebuttal=bear_rebuttal_r,
        duration_s=round(time.perf_counter() - started, 1),
        suggested_size_usd=size_usd,
        risk_flags=risk_flags,
        past_decisions=past["past_decisions"] if past else [],
        token_usage=token_totals,
        as_of=market.as_of,
        providers={str(key): str(value) for key, value in market.sources.items()},
        source_references=_source_references(market),
    )
    logger.info(
        "[analysis] %s completed in %.1fs (%s)", ticker, analysis.duration_s, decision
    )
    # Record the decision for future reflection (ROADMAP 1.1). A failure here
    # must never surface to the user or block the result. Backtest replays
    # never write - they are not live decisions.
    if analysis.error is None and live_context:
        try:
            from app import memory

            await memory.record_decision(run_id, analysis)
        except Exception as exc:  # noqa: BLE001 - best-effort like the portfolio
            logger.warning("[memory] could not record decision: %s", exc)
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
