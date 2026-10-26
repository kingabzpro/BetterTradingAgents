"""Analysis workflow: 5 parallel research agents -> bull+bear in parallel -> manager.

Every agent run is one single completion through the OpenAI Agents SDK against
an OpenAI-compatible endpoint. One agent failing never kills the run - its
slot becomes None and downstream agents are told the input is missing. Agents
get no output_type on purpose: the SDK's structured-output path forces a
strict json_schema request that some OpenAI-compatible providers (e.g.
api.deepseek.com) reject with a 400 on every call, so JSON stays a prompt
contract and every reply is parsed by extract_json. With STREAM_REASONING on,
each agent's tokens stream to the UI as agent_token events (ROADMAP 3.2);
mock mode streams the deterministic text word-by-word so the UI path is
always testable.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from app import changes, cost, quality, risk
from app.agents import (
    bear,
    bull,
    forecast,
    fundamental,
    judge,
    manager,
    market as market_regime,
    news,
    sentiment,
    technical,
)
from app.config import safe_base_url, settings
from app.depth import DEFAULT_DEPTH, depth_profile
from app.models import (
    AgentResult,
    PortfolioPosition,
    PortfolioSummary,
    PreviousCall,
    SourceReference,
    StockAnalysis,
)
from app.outlook import DEFAULT_OUTLOOK, horizon_days as outlook_horizon, user_context
from app.tools.indicators import compute_indicators, historical_forecast
from app.tools.market_data import (
    MarketData,
    get_hosted_forecast,
    get_market_context,
    get_stock_data,
)

logger = logging.getLogger("analysis")

Emit = Callable[[str, dict], Awaitable[None]]

# Live reasoning stream (ROADMAP 3.2). The UI pane keeps a sliding window per
# agent; STREAM_MAX_CHARS guards the event volume of a runaway stream.
STREAM_WINDOW_CHARS = 2048
STREAM_MAX_CHARS = 16_384
_MOCK_STREAM_WORDS = 5  # mock mode: words per streamed chunk
_MOCK_STREAM_DELAY = 0.025  # seconds between chunks

# Per-role LLM overrides (ROADMAP 2.3) resolve through app.config.AGENT_ROLES
# - cheap fast models for the researchers, a stronger one for the call - and
# per-agent overrides (settings.llm_key_for) layer on top of the role.

_llms: dict[str, Any] = {}  # role -> AppLLM wrapper (None in mock mode)
_llm_roles_initialized: set[str] = set()
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_BACKOFF_SECONDS = (1.0, 4.0, 16.0, 60.0)  # HTTP 429/5xx: wait, retry, wait longer

# LLM_CONCURRENCY: shared endpoints cap concurrent requests per workspace
# (e.g. Modal's shared DeepSeek endpoints at 16), and a multi-ticker run can
# burst past that. One app-wide gate rebuilds live when the setting changes,
# like the per-role LLM cache does.
_llm_gate: asyncio.Semaphore | None = None
_llm_gate_limit = -1


def _gate() -> asyncio.Semaphore | None:
    global _llm_gate, _llm_gate_limit
    limit = max(0, int(settings.llm_concurrency or 0))
    if limit != _llm_gate_limit:
        _llm_gate = asyncio.Semaphore(limit) if limit else None
        _llm_gate_limit = limit
    return _llm_gate


# Provider prefixes that only ever existed for litellm routing. The SDK client
# is pointed at one OpenAI-compatible base_url, so the prefix must be stripped
# from the wire model name ("openai/gpt-x" -> "gpt-x"); anything else
# (bare names and org/model forms like zai-org/GLM-5.3) goes through verbatim.
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


def _api_model_name(model: str) -> str:
    for prefix in _KNOWN_PROVIDER_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


@dataclass
class AppLLM:
    """One role's LLM: an AsyncOpenAI client pinned to the role's endpoint plus
    the SDK model and request settings built from it. Plain attributes on
    purpose: the offline checks assert model/base_url/max_tokens/stream and
    the retry loop rebuilds roles by swapping this object in the cache."""

    model: str  # wire model name (provider prefix stripped)
    base_url: str
    max_tokens: int | None
    stream: bool
    additional_params: dict  # reasoning_effort / response_format mirror
    model_obj: Any  # OpenAIChatCompletionsModel
    model_settings: Any  # agents.ModelSettings


def _make_llm(
    model: str,
    base_url: str,
    api_key: str,
    temperature: float,
    timeout: float,
    max_tokens: int | None,
    reasoning_effort: str | None,
    stream: bool,
    additional_params: dict,
) -> AppLLM:
    """Construct the SDK stack for one role. Own function so the offline
    checks can patch it and assert on the exact request kwargs."""
    from agents import ModelSettings
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=0,  # the app's own retry loop owns backoff + classification
    )
    model_obj = OpenAIChatCompletionsModel(model=model, openai_client=client)
    # include_usage matters on custom base_urls: without it a streamed call
    # reports requests=1 and zero tokens (the SDK only adds stream_options for
    # the official OpenAI endpoint).
    model_settings = ModelSettings(
        temperature=temperature,
        max_tokens=max_tokens,
        include_usage=True,
        extra_args=additional_params,
    )
    return AppLLM(
        model=model,
        base_url=base_url,
        max_tokens=max_tokens,
        stream=stream,
        additional_params=additional_params,
        model_obj=model_obj,
        model_settings=model_settings,
    )


def _build_llm(
    role: str, json_mode: bool = True, stream: bool | None = None,
    reasoning_effort: str | None = None,
) -> AppLLM:
    """Construct the LLM wrapper for one role from settings.llm_for(role).

    json_mode asks the provider for guaranteed JSON output; reasoning_effort
    is sent verbatim in the request body. stream=None follows
    settings.stream_reasoning (ROADMAP 3.2 live token events).
    """
    conf = settings.llm_for(role)
    # P2.11: enforce at the client too, not only in Settings: a hand-edited
    # .env must not quietly point the API key at a cleartext remote host.
    if not safe_base_url(conf["base_url"]):
        raise ValueError(
            f"llm base_url for {role} is neither https nor loopback http: "
            f"{conf['base_url']!r}; refusing to send the API key there"
        )
    effort = reasoning_effort if reasoning_effort is not None else (
        settings.llm_reasoning_effort_manager if role == "manager" else ""
    ) or settings.llm_reasoning_effort
    extra: dict = {}
    if effort:
        extra["reasoning_effort"] = effort
    if json_mode:
        extra["response_format"] = {"type": "json_object"}
    if stream is None:
        stream = settings.stream_reasoning
    return _make_llm(
        model=_api_model_name(conf["model"]),
        base_url=conf["base_url"],
        api_key=conf["api_key"],
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
        max_tokens=settings.llm_max_tokens or None,  # P2.18: 0 = provider default
        reasoning_effort=effort or None,
        stream=stream,
        additional_params=extra,
    )


def get_llm(key: str = "analysts") -> Any:
    """Build (once) and cache the LLM wrapper for one key: a role, or an
    agent name when that agent overrides its role's endpoint config.

    Roles: manager / analysts / debate, resolved via settings.llm_for -
    per-role env overrides fall back to the global LLM_* values, per-agent
    overrides beat both. Returns None when no LLM is configured (mock mode).
    """
    if key not in _llm_roles_initialized:
        _llm_roles_initialized.add(key)
        if settings.llm_configured:
            try:
                _llms[key] = _build_llm(key)
            except Exception as exc:  # noqa: BLE001 - degrade to mock, don't crash
                logger.error("[llm] could not build LLM for %s: %s", key, exc)
                _llms[key] = None
        else:
            _llms[key] = None
    return _llms.get(key)


def get_chat_llm() -> Any:
    """LLM for the follow-up manager chat: the manager role's model, but plain
    prose - no JSON mode, no streaming (the chat is request/response, not a
    live token pane). Returns None when no LLM is configured (mock mode)."""
    if "chat" not in _llm_roles_initialized:
        _llm_roles_initialized.add("chat")
        if settings.llm_configured:
            try:
                _llms["chat"] = _build_llm(
                    "manager", json_mode=False, stream=False,
                    reasoning_effort=settings.llm_reasoning_effort,
                )
            except Exception as exc:  # noqa: BLE001 - degrade to mock, don't crash
                logger.error("[llm] could not build chat LLM: %s", exc)
                _llms["chat"] = None
        else:
            _llms["chat"] = None
    return _llms.get("chat")


def _drop_json_mode(key: str) -> Any:
    """Rebuild a key's LLM without response_format after the provider
    rejected it (feature-detect on first use, not with a probe call)."""
    try:
        _llms[key] = _build_llm(key, json_mode=False)
        logger.warning(
            "[llm] JSON mode disabled for %s - provider rejected it", key
        )
        return _llms[key]
    except Exception as exc:  # noqa: BLE001
        logger.error("[llm] could not rebuild LLM for %s: %s", key, exc)
        return None


def _drop_stream_mode(key: str) -> Any:
    """Rebuild a key's LLM without streaming after the provider rejected it
    (feature-detect on first use, exactly like the JSON-mode fallback). The
    JSON-mode setting of the current LLM is preserved."""
    current = _llms.get(key)
    json_mode = current is None or "response_format" in (
        getattr(current, "additional_params", None) or {}
    )
    try:
        _llms[key] = _build_llm(key, json_mode=json_mode, stream=False)
        logger.warning("[llm] streaming disabled for %s - provider rejected it", key)
        return _llms[key]
    except Exception as exc:  # noqa: BLE001
        logger.error("[llm] could not rebuild LLM for %s: %s", key, exc)
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


def _usage_snapshot(usage: Any) -> dict:
    """Token usage of one SDK run, tolerating missing provider metrics."""
    if usage is None:
        return {}
    prompt = int(getattr(usage, "input_tokens", 0) or 0)
    completion = int(getattr(usage, "output_tokens", 0) or 0)
    reasoning = int(
        getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
    )
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
    """One agent call: a single completion, no tools, so the SDK terminates
    after exactly one model request. Returns (parsed_json_dict, usage)."""
    from agents import Runner

    result = await asyncio.wait_for(
        Runner.run(agent, task.description), timeout=settings.llm_timeout_seconds
    )
    usage = _usage_snapshot(result.context_wrapper.usage)
    return extract_json(str(result.final_output or "")), usage


async def _kick_streamed(
    agent, task, ticker: str, agent_name: str, emit: Emit,
) -> tuple[dict, dict]:
    """Streaming variant of _kick_once: text deltas are forwarded through the
    app's token sink (same contract crewai's scoped sinks had), and usage is
    read from the run context after the stream drains."""
    from agents import Runner
    from agents.stream_events import RawResponsesStreamEvent
    from openai.types.responses.response_text_delta_event import (
        ResponseTextDeltaEvent,
    )

    sink = _token_sink(ticker, agent_name, emit)
    streamed = Runner.run_streamed(agent, task.description)

    async def _drain() -> None:
        async for event in streamed.stream_events():
            if (
                isinstance(event, RawResponsesStreamEvent)
                and isinstance(event.data, ResponseTextDeltaEvent)
                and event.data.delta
            ):
                sink(None, SimpleNamespace(type="llm_stream_chunk", chunk=event.data.delta))

    await asyncio.wait_for(_drain(), timeout=settings.llm_timeout_seconds)
    usage = _usage_snapshot(streamed.context_wrapper.usage)
    return extract_json(str(streamed.final_output or "")), usage


def _token_sink(ticker: str, agent_name: str, emit: Emit) -> Callable[[Any, Any], None]:
    """Token sink that forwards one agent's streamed deltas as agent_token events.

    One sink per agent run, so concurrent agents never receive each other's
    tokens. Only content chunks are forwarded (thinking deltas stay private),
    a sliding window is kept for debugging, and STREAM_MAX_CHARS caps the
    total so a runaway stream cannot flood the event queues. Chunks may in
    principle arrive off the loop thread, so forwarding goes through the loop
    captured here + call_soon_threadsafe.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # defensive: _run_agent always runs inside a loop
        loop = None
    state = {"sent": 0, "window": ""}

    def _forward(payload: dict) -> None:
        loop.create_task(emit("agent_token", payload))

    def sink(source: Any, event: Any) -> None:  # noqa: ARG001 - sink contract
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


# The Market Analyst reads index-level stats only (no ticker data), so one
# regime read answers for every ticker that sees the same indices snapshot.
# Keyed by payload content plus model identity, never by time: a new trading
# day's numbers miss the cache naturally, and identical input under the same
# model can never be a stale read. Values are single-flight tasks, so the
# concurrent siblings of one run join the in-flight call instead of each
# paying the LLM. Bounded because intraday refreshes jitter the numbers and
# every distinct snapshot would otherwise stay forever.
_market_regime_cache: OrderedDict[str, asyncio.Task] = OrderedDict()
_REGIME_CACHE_MAX = 16


def _regime_cache_key(indices: dict) -> str:
    # Settings apply live without a restart: a model switch (or mock -> real)
    # under an unchanged snapshot must not be answered by the old model's read.
    llm_key = settings.llm_key_for(market_regime.NAME)
    llm = get_llm(llm_key)
    identity = "mock" if llm is None else f"{llm_key}:{settings.llm_for(llm_key)['model']}"
    return hashlib.sha256(
        json.dumps([indices, identity], sort_keys=True, default=str).encode()
    ).hexdigest()


async def _noop_emit(kind: str, payload: dict) -> None:  # noqa: ARG001
    """Sink for samples whose events are folded into one stage event."""


def _agent_completed_payload(
    ticker: str,
    agent: str,
    data: dict,
    duration_s: float,
    tokens: int,
    cached: bool = False,
) -> dict:
    event = {
        "ticker": ticker,
        "agent": agent,
        "duration_s": round(duration_s, 1),
        "signal": data.get("signal") or data.get("decision"),
        "confidence": data.get("confidence") or data.get("score"),
        "summary": str(data.get("summary", ""))[:200],
        "tokens": tokens,
    }
    if cached:
        event["cached"] = True
    return event


async def _run_agent(
    mod,
    ticker: str,
    emit: Emit,
    name: str | None = None,
    token_totals: dict | None = None,
    role_usage: dict | None = None,
    live: bool = True,
    **task_payload,
) -> dict | None:
    """Run one agent (LLM or mock), emitting started/completed/failed events.

    LLM failures get one smart retry each (ROADMAP 2.3): 429/5xx waits and
    retries (1s, then 4s), a rejected response_format disables JSON mode for
    the role, a rejected stream disables streaming for the role, and a
    malformed output is retried once with the validation error appended to
    the task. `token_totals` accumulates per-run usage; `role_usage` records
    it per role (manager / analysts / debate) so the cost estimate can price
    each role's own model; `live` gates the cosmetic token stream (backtest
    replays skip it).
    """
    agent_name = name or mod.NAME
    # LLM identity: the agent's own override when configured, else its role
    # (agents without an override share the role's LLM object).
    llm_key = settings.llm_key_for(mod.NAME)
    await emit("agent_started", {"ticker": ticker, "agent": agent_name})
    started = time.perf_counter()
    try:
        llm = get_llm(llm_key)
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
            retries = settings.llm_max_retries  # P2.18: bound the retry budget
            for attempt in range(1, retries + 2):
                try:
                    agent = mod.build_agent(llm)
                    task = mod.build_task(agent, ticker, **task_payload)
                    if correction is not None:
                        task.description = correction + task.description
                    async def _kick() -> tuple[dict, dict]:
                        if getattr(llm, "stream", False):
                            # One sink per agent run: concurrent agents of the
                            # same role never see each other's tokens (ROADMAP 3.2).
                            return await _kick_streamed(
                                agent, task, ticker, agent_name, emit
                            )
                        return await _kick_once(agent, task)

                    gate = _gate()
                    if gate is None:
                        data, usage = await _kick()
                    else:
                        async with gate:
                            data, usage = await _kick()
                    # Let token-forward tasks scheduled by the sink reach the
                    # queues before agent_completed closes the pane's stream.
                    await asyncio.sleep(0)
                    break
                except Exception as exc:  # noqa: BLE001 - classify, maybe retry
                    last_error = exc
                    kind = _classify_failure(exc)
                    detail = str(exc)[:120]
                    if kind == "stream":
                        replacement = _drop_stream_mode(llm_key)
                        if replacement is None:
                            break
                        logger.warning(
                            "[%s] provider rejected streaming; retrying without it",
                            agent_name,
                        )
                        llm = replacement
                        continue
                    if kind == "response_format":
                        replacement = _drop_json_mode(llm_key)
                        if replacement is None:
                            break
                        logger.warning(
                            "[%s] provider rejected JSON mode; retrying without it",
                            agent_name,
                        )
                        llm = replacement
                        continue
                    if kind in ("rate_limit", "server") and attempt < retries + 1:
                        wait = _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]
                        logger.warning(
                            "[%s] %s error (%s); retry %d/%d in %.0fs",
                            agent_name,
                            kind,
                            detail,
                            attempt,
                            retries,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    if kind == "bad_output" and correction is None and attempt < retries + 1:
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
        if role_usage is not None and usage:
            entry = role_usage.setdefault(
                llm_key,
                {
                    "model": settings.llm_for(llm_key)["model"],
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            )
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                entry[key] += int(usage.get(key, 0) or 0)
        await emit(
            "agent_completed",
            _agent_completed_payload(
                ticker, agent_name, data, duration, usage.get("total_tokens", 0)
            ),
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
    """Paper-account holdings for the manager prompt and the risk gate.

    The Alpaca paper account is the only portfolio: cash, equity, and open
    positions come straight from the broker. Best-effort like any provider:
    an unconfigured account (or a broker failure) yields None, and the manager
    then weighs the research without a holdings section. Multi-ticker runs
    fetch this once and share it (see app.runs) instead of re-fetching every
    position once per ticker.
    """
    try:
        from app import broker

        if not (await broker.status()).configured:
            return None
        account, held = await asyncio.gather(broker.account(), broker.positions())
        # Read after account(): the ratchet inside it has already folded this
        # read's equity into the mark the drawdown brake compares against.
        mark = await broker.equity_mark()
        positions = [
            PortfolioPosition(
                id=index,
                ticker=position.symbol,
                quantity=position.quantity,
                entry_price=position.avg_entry_price,
                current_price=position.current_price,
                cost=round(position.avg_entry_price * position.quantity, 2),
                value=position.market_value,
                pnl=position.unrealized_pl,
                pnl_pct=round(position.unrealized_plpc * 100, 2)
                if position.unrealized_plpc is not None
                else None,
                external=True,
            )
            for index, position in enumerate(held, start=1)
        ]
        known_value = sum(p.value for p in positions if p.value is not None)
        return PortfolioSummary(
            cash=account.cash or 0.0,
            positions_value=round(known_value, 2),
            total_equity=account.equity,
            total_pnl=round(sum(p.pnl or 0.0 for p in positions), 2),
            equity_high_water=mark.high_water,
            unpriced_count=sum(1 for p in positions if p.value is None),
            positions=positions,
        )
    except Exception as exc:  # noqa: BLE001 - portfolio context is best-effort
        logger.warning("[portfolio] paper summary failed: %s", exc)
        return None


async def fetch_held_closes(
    portfolio: PortfolioSummary | None,
) -> dict[str, list[float]]:
    """Daily closes for every held ticker, for the concentration gate (P1.3).

    Best-effort like the portfolio itself: a ticker whose history cannot be
    fetched is simply absent, and the gate then reports its correlation as
    unknown instead of assuming independence. Multi-ticker runs fetch this
    once and share it (see app.runs).
    """
    if portfolio is None or not portfolio.positions:
        return {}
    tickers = sorted({p.ticker for p in portfolio.positions})
    from app.tools.market_data import get_closes

    histories = await asyncio.gather(
        *(get_closes(t) for t in tickers), return_exceptions=True
    )
    closes: dict[str, list[float]] = {}
    for held, history in zip(tickers, histories):
        if isinstance(history, BaseException):
            logger.warning("[risk] %s price history failed: %s", held, history)
        elif history:
            closes[held] = history
    return closes


async def fetch_past_decisions(ticker: str) -> dict | None:
    """Graded past calls on this ticker + cross-ticker lessons; best-effort."""
    try:
        from app import memory

        return await memory.get_reflections(ticker)
    except Exception as exc:  # noqa: BLE001 - memory context is best-effort
        logger.warning("[memory] reflections failed: %s", exc)
        return None


async def fetch_analyst_track_record(live: bool) -> str:
    """Per-analyst graded hit rates for the judge, best-effort (SQLite only).

    Replay runs read no live history (point-in-time rule) and incognito runs
    read nothing at all; both get an explicit unavailable marker instead of an
    empty string so the judge can tell "no record" from "lookup broke".
    """
    if not live:
        return "unavailable in this run (replay or incognito: no graded history is read)"
    try:
        from app import calibration

        scorecard = await asyncio.to_thread(calibration.analyst_scorecard)
        return calibration.scorecard_text(scorecard)
    except Exception as exc:  # noqa: BLE001 - track record is best-effort
        logger.warning("[judge] analyst track record lookup failed: %s", exc)
        return "UNAVAILABLE - analyst track record lookup failed"


def _trim_news_items(items: list[dict]) -> list[dict]:
    """Bound the news payload a researcher reads (release batch 2, item 8).

    Caps the item count and truncates summary/content fields for the prompt
    only; the UI provenance (source_references) still carries the fuller
    list. PROMPT_TRIM=0 restores the untrimmed payload for A/B runs.
    """
    if not settings.prompt_trim:
        return items
    trimmed = []
    for item in items[: settings.news_prompt_items]:
        out = dict(item)
        summary = out.get("summary")
        if isinstance(summary, str) and len(summary) > settings.news_prompt_summary_chars:
            out["summary"] = summary[: settings.news_prompt_summary_chars].rstrip() + "…"
        content = out.get("content")
        if isinstance(content, str) and len(content) > settings.news_prompt_content_chars:
            out["content"] = content[: settings.news_prompt_content_chars].rstrip() + "…"
        trimmed.append(out)
    return trimmed


def _portfolio_context(
    summary: PortfolioSummary | None, incognito: bool = False
) -> list[dict] | str:
    """Compact view of open positions for the manager prompt."""
    if incognito:
        return "NOT SHARED - incognito run: no portfolio access"
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


# Soft per-stage budgets (seconds) behind the ROADMAP P2.14 slow-stage
# warning; researchers share the fallback budget.
STAGE_BUDGETS_S = {"data_fetch": 60, "debate": 240, "judge": 120, "manager": 240}
RESEARCH_STAGE_BUDGET_S = 180


def over_budget(stage: str, seconds: float) -> bool:
    return seconds > STAGE_BUDGETS_S.get(stage, RESEARCH_STAGE_BUDGET_S)


async def analyze_ticker(
    ticker: str,
    emit: Emit,
    portfolio_summary: PortfolioSummary | None = None,
    outlook: str = DEFAULT_OUTLOOK,
    depth: str = DEFAULT_DEPTH,
    run_id: str = "",
    market_data: MarketData | None = None,
    live_context: bool = True,
    exclude_analysts: tuple[str, ...] | list[str] = (),
    held_closes: dict[str, list[float]] | None = None,
    previous: PreviousCall | None = None,
    incognito: bool = False,
) -> StockAnalysis:
    """Full 3-stage workflow for one ticker.

    `portfolio_summary` lets a multi-ticker run fetch the portfolio once and
    share it across tickers; when omitted it is fetched here. `outlook` is the
    user's chosen horizon (day_trade / short_term / long_term) and is injected
    into every agent payload so the whole crew weighs evidence for that horizon.
    `depth` (fast / pro / max) selects which researchers run and whether
    the debate judge cross-examines bull and bear - fewer agents, faster run. `run_id` tags
    the decision-memory row written at the end of a successful run.
    `market_data` injects a pre-built snapshot (backtests replay date T with
    only data known at T); `live_context=False` is the backtest mode: no
    portfolio fetch, no decision-memory lookup or recording - both would leak
    information from after the replayed date. `exclude_analysts` drops
    researchers whose inputs have no point-in-time source (P1.2: the
    fundamental analyst in replays), from prompts, events, and coverage.
    `held_closes` carries pre-fetched daily closes for the held tickers (the
    concentration gate's correlation input); when omitted it is fetched here.
    `previous` is the earlier completed call this one is compared against for
    the deterministic `What changed` summary (P1.5); backtests pass none.
    `incognito` runs market-only like a live run but without any private
    context: no portfolio fetch, no decision-memory lookup or recording. It
    keeps live token streaming (unlike backtests).
    """
    started = time.perf_counter()
    await emit("ticker_started", {"ticker": ticker})
    # Incognito logs stay anonymous: which ticker was analyzed is part of the
    # report the user may be trying not to leave behind.
    logger.info("[analysis] Starting %s", "an incognito ticker" if incognito else ticker)

    # Per-stage wall times (ROADMAP P2.14): recorded for every stage, surfaced
    # beside the run's cost estimate, flagged in the log when over budget.
    # Researchers overlap, so the sum can exceed the run's wall time.
    stage_times: dict[str, float] = {}

    async def timed(stage: str, coro):
        begin = time.perf_counter()
        try:
            return await coro
        finally:
            stage_times[stage] = round(time.perf_counter() - begin, 1)

    # Depth profile, horizon, and exclusions are pure config - resolved before
    # the data fetch so the forecast hook can be handed to get_stock_data.
    prof = depth_profile(depth)
    # The run's horizon drives the forecast, the risk band, and grading.
    run_horizon = outlook_horizon(outlook)
    # Backtests can drop researchers whose data has no point-in-time source
    # (P1.2: fundamentals); the exclusion applies to prompts, events, and the
    # data-quality coverage report alike.
    research: tuple[str, ...] = tuple(
        key for key in prof["research"] if key not in exclude_analysts
    )
    # The hosted forecast only needs the closes, so it starts the moment the
    # price history lands inside get_stock_data instead of after the whole
    # gather (ROADMAP P2.14).
    forecast_hook = (
        (lambda closes: get_hosted_forecast(closes, run_horizon, ticker))
        if "forecast" in research
        else None
    )

    if market_data is not None:
        market = market_data
        stage_times["data_fetch"] = 0.0
        if market.price is None:
            await emit("ticker_failed", {"ticker": ticker, "error": "no price in snapshot"})
            return StockAnalysis(ticker=ticker, error="no price in snapshot")
    else:
        try:
            market = await timed(
                "data_fetch", get_stock_data(ticker, forecast=forecast_hook)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[analysis] %s aborted: market data failed: %s", ticker, exc)
            await emit("ticker_failed", {"ticker": ticker, "error": str(exc)[:200]})
            return StockAnalysis(
                ticker=ticker, error=f"market data failed: {exc}"[:300]
            )

    indicators = compute_indicators(
        market.closes, market.highs, market.lows, market.volumes
    )
    # Market-regime context for the Market Analyst: fetched once, only when
    # the profile asks for the agent, overlapping the forecast fetch below.
    indices_task = (
        asyncio.create_task(get_market_context())
        if "market" in research
        else None
    )
    local_forecast: dict | None = None
    hosted_forecast = market.hosted_forecast
    if "forecast" in research:
        local_forecast = historical_forecast(market.closes, run_horizon)
        if hosted_forecast is None and market_data is not None:
            # Injected snapshot (backtest replay): no gather to overlap, so
            # the hosted chain runs here on the replayed closes. Any failure
            # falls through; the payload key keeps its historical name for
            # saved-run parity.
            hosted_forecast = await get_hosted_forecast(
                market.closes, run_horizon, ticker
            )
        if hosted_forecast:
            indicators.update(hosted_forecast)
            market.sources["forecast"] = "bako"
        else:
            # Replace compute_indicators' default-5-day projection so the
            # stored analysis matches the run's horizon when Bako is absent.
            if local_forecast:
                indicators.update(local_forecast)
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
    role_usage: dict[str, dict] = {}
    user_ctx = user_context(outlook)
    noise_band_5d = risk.forecast_noise_pct(
        indicators.get("volatility_annualized_pct"), run_horizon
    )
    forecast_payload = {
        "price": market.price,
        "history_days": len(market.closes),
        "hosted_forecast": hosted_forecast,
        "local_trend_forecast": local_forecast,
        # Precomputed so the analyst quotes the same band the risk gate uses
        # for this outlook's horizon, not the raw annualized volatility.
        "noise_band_pct": round(noise_band_5d, 2) if noise_band_5d is not None else None,
        "horizon_days": run_horizon,
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
        if key == "market":
            indices = await indices_task if indices_task else {}
            if not indices:
                # No index data at all: the regime read would be invention.
                await emit(
                    "agent_failed",
                    {
                        "ticker": ticker,
                        "agent": "market",
                        "error": "skipped: market index data unavailable",
                    },
                )
                return None
            # One regime read per identical index snapshot: the payload holds
            # no ticker data, so sibling tickers reuse it instead of paying
            # the LLM again - concurrent siblings join the in-flight task.
            # Incognito shares no state, read or write.
            regime_key = "" if incognito else _regime_cache_key(indices)
            flight = _market_regime_cache.get(regime_key) if regime_key else None
            if flight is not None:
                # Announce "running" up front: a joiner waits on the
                # initiator's call, and a blank row during that wait reads as
                # if the agent never ran.
                await emit(
                    "agent_started", {"ticker": ticker, "agent": market_regime.NAME}
                )
                try:
                    hit = await flight
                except asyncio.CancelledError:
                    hit = None  # the flight died with an aborted run; retry
                if hit is None:
                    _market_regime_cache.pop(regime_key, None)
                else:
                    data = {**hit["result"], "ticker": ticker}
                    await emit(
                        "agent_completed",
                        _agent_completed_payload(
                            ticker, market_regime.NAME, data, hit["duration_s"], 0, cached=True
                        ),
                    )
                    return data

            async def _regime_once() -> dict | None:
                started = time.perf_counter()
                result = await _run_agent(
                    market_regime,
                    ticker,
                    emit,
                    token_totals=token_totals,
                    role_usage=role_usage,
                    live=live_context,
                    payload=indices,
                )
                if not result:
                    return None
                # Copy so no caller can mutate the dict the cache shares. The
                # initiator's wall time rides along so reused reads display
                # the same duration the call took, not a blank.
                return {"result": {**result}, "duration_s": time.perf_counter() - started}

            if not regime_key:
                outcome = await _regime_once()
                return outcome["result"] if outcome else None
            # No await between the miss check and this store: siblings in the
            # same loop are guaranteed to see the flight, not start their own.
            flight = asyncio.ensure_future(_regime_once())
            _market_regime_cache[regime_key] = flight
            while len(_market_regime_cache) > _REGIME_CACHE_MAX:
                _market_regime_cache.popitem(last=False)
            try:
                outcome = await flight
            except asyncio.CancelledError:
                _market_regime_cache.pop(regime_key, None)
                raise
            if not outcome:
                # A failed run is never cached; drop the flight so a later
                # ticker retries instead of joining a dead result.
                _market_regime_cache.pop(regime_key, None)
            return {**outcome["result"]} if outcome else None
        if key == "technical":
            return await _run_agent(
                technical,
                ticker,
                emit,
                token_totals=token_totals,
                role_usage=role_usage,
                live=live_context,
                payload={**indicators, "user_context": user_ctx},
            )
        if key == "fundamental":
            return await _run_agent(
                fundamental,
                ticker,
                emit,
                token_totals=token_totals,
                role_usage=role_usage,
                live=live_context,
                payload={**market.fundamentals, "user_context": user_ctx},
            )
        if key == "news":
            return await _run_agent(
                news,
                ticker,
                emit,
                token_totals=token_totals,
                role_usage=role_usage,
                live=live_context,
                payload={"items": _trim_news_items(market.news), "user_context": user_ctx},
            )
        if key == "sentiment":
            social_items = (
                market.social[: settings.social_prompt_items]
                if settings.prompt_trim
                else market.social
            )
            return await _run_agent(
                sentiment,
                ticker,
                emit,
                token_totals=token_totals,
                role_usage=role_usage,
                live=live_context,
                payload={
                    "items": social_items,
                    "crowd_proxy": market.crowd_proxy,
                    "user_context": user_ctx,
                },
            )
        return await _run_agent(
            forecast,
            ticker,
            emit,
            token_totals=token_totals,
            role_usage=role_usage,
            live=live_context,
            payload=forecast_payload,
        )

    order = ("market", "technical", "fundamental", "news", "forecast", "sentiment")
    research_keys = [key for key in order if key in research]
    research_data = dict(
        zip(
            research_keys,
            await asyncio.gather(*(timed(key, run_research(key)) for key in research_keys)),
        )
    )
    market_data_res = research_data.get("market")
    tech_data = research_data.get("technical")
    fund_data = research_data.get("fundamental")
    news_data = research_data.get("news")
    sentiment_data = research_data.get("sentiment")
    forecast_data = research_data.get("forecast")
    market_r = market_regime.to_result(market_data_res, ticker) if market_data_res else None
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
        if key in prof["research"]:
            return "SKIPPED - excluded from this replay (no point-in-time data source)"
        return f"SKIPPED - not requested in the {prof['label']} depth profile"

    context = {
        "ticker": ticker,
        "price": market.price,
        "user_context": user_ctx,
        "market": slot("market", market_r),
        "technical": slot("technical", tech),
        "fundamental": slot("fundamental", fund),
        "news": slot("news", news_r),
        "sentiment": slot("sentiment", sentiment_r),
        "forecast": slot("forecast", forecast_r),
    }

    # Stage 2: bull and bear in parallel. Fast depth skips the debate
    # entirely; the manager then weighs the research directly (its payload
    # marks the debate SKIPPED via slot()).
    bull_data, bear_data = None, None
    if prof.get("debate", True):
        bull_data, bear_data = await timed(
            "debate",
            asyncio.gather(
                _run_agent(
                    bull, ticker, emit, token_totals=token_totals, role_usage=role_usage,
                    live=live_context, payload=context,
                ),
                _run_agent(
                    bear, ticker, emit, token_totals=token_totals, role_usage=role_usage,
                    live=live_context, payload=context,
                ),
            ),
        )

    # Stage 2b: the judge cross-examines both round-1 cases against the
    # research and returns calibrated strengths plus a verdict (one neutral
    # call replacing the old self-scored rebuttal round). Only expert depth
    # asks for it (and the server must allow DEBATE_ROUNDS >= 2); it is
    # skipped when either first-round brief failed.
    judge_data = None
    if prof.get("judge") and settings.debate_rounds >= 2:
        if bull_data and bear_data:
            analyst_record = await fetch_analyst_track_record(
                live=live_context and not incognito
            )
            judge_data = await timed(
                "judge",
                _run_agent(
                    judge,
                    ticker,
                    emit,
                    token_totals=token_totals,
                    role_usage=role_usage,
                    live=live_context,
                    payload={
                        "research": context,
                        "bull_round_1": bull_data,
                        "bear_round_1": bear_data,
                        "analyst_track_record": analyst_record,
                    },
                ),
            )
        else:
            await emit(
                "agent_failed",
                {
                    "ticker": ticker,
                    "agent": "judge",
                    "error": "skipped: first-round debate incomplete",
                },
            )

    bull_r = bull.to_result(bull_data, ticker) if bull_data else None
    bear_r = bear.to_result(bear_data, ticker) if bear_data else None
    judge_r = judge.to_result(judge_data, ticker) if judge_data else None

    # Stage 3: portfolio manager (sees existing holdings so decisions account
    # for exposure already taken; the debate transcript shows how the final
    # positions were reached). Backtest replay skips live context entirely;
    # incognito is live but private-blind: no portfolio, no decision memory,
    # on either side (read or write).
    if incognito:
        portfolio_summ = None
        past = {"past_decisions": [], "cross_ticker_lessons": []}
    elif live_context:
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
    # Standardize the forecast against the stock's own noise band for this
    # run's horizon, so the manager and the risk gate read the same number.
    # (see app.risk)
    vol_ann_pct = indicators.get("volatility_annualized_pct")
    forecast_change_pct = indicators.get("forecast_change_5d_pct")
    forecast_band_pct = risk.forecast_noise_pct(vol_ann_pct, run_horizon)
    forecast_z_value = risk.forecast_z(forecast_change_pct, vol_ann_pct, run_horizon)
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
            "rounds": 2 if (prof.get("judge") and settings.debate_rounds >= 2) else 1,
            "bull_round_1": bull_data,
            "bear_round_1": bear_data,
            "judge": judge_data,
        },
        "current_portfolio": _portfolio_context(portfolio_summ, incognito),
        "past_decisions": past_decisions_ctx,
        "cross_ticker_lessons": cross_lessons_ctx,
    }
    # Item 14: manager self-consistency voting. With MANAGER_VOTES > 1 (and a
    # real LLM configured; mock mode is deterministic, so one sample is the
    # same answer) the final call runs N times in parallel and the majority
    # wins. Samples run emit-less and the stage emits one started/completed
    # pair around the vote, so the UI keeps one manager row carrying the
    # winning payload; per-call usage still rolls up for cost either way.
    manager_votes: list[str] = []
    manager_agreement: float | None = None
    votes_n = settings.manager_votes if settings.llm_configured else 1
    if votes_n > 1:
        await emit("agent_started", {"ticker": ticker, "agent": manager.NAME})
        stage_started = time.perf_counter()
        tokens_before = int(token_totals.get("total_tokens", 0))

        async def _vote_sample() -> dict | None:
            try:
                return await _run_agent(
                    manager,
                    ticker,
                    _noop_emit,
                    token_totals=token_totals,
                    role_usage=role_usage,
                    live=live_context,
                    payload=full_context,
                )
            except Exception as exc:  # noqa: BLE001 - one bad sample must not sink the vote
                logger.warning("[manager] vote sample failed: %s", exc)
                return None

        samples = await timed(
            "manager", asyncio.gather(*(_vote_sample() for _ in range(votes_n)))
        )
        mgr_data, manager_votes, manager_agreement = manager.majority_vote(
            [sample for sample in samples if sample], ticker
        )
        stage_tokens = int(token_totals.get("total_tokens", 0)) - tokens_before
        if mgr_data is None:
            await emit(
                "agent_failed",
                {
                    "ticker": ticker,
                    "agent": manager.NAME,
                    "error": f"all {votes_n} vote samples failed",
                },
            )
        else:
            await emit(
                "agent_completed",
                _agent_completed_payload(
                    ticker,
                    manager.NAME,
                    mgr_data,
                    time.perf_counter() - stage_started,
                    stage_tokens,
                ),
            )
    else:
        mgr_data = await timed(
            "manager",
            _run_agent(
                manager,
                ticker,
                emit,
                token_totals=token_totals,
                role_usage=role_usage,
                live=live_context,
                payload=full_context,
            ),
        )

    # ROADMAP P2.14: flag stages that blew their budget so "which stage was
    # slow" is one log glance. No ticker in the line - incognito logs stay
    # anonymous.
    for stage, seconds in stage_times.items():
        if over_budget(stage, seconds):
            logger.warning(
                "[analysis] stage over budget: %s took %.1fs",
                stage,
                seconds,
            )

    if mgr_data:
        final = manager.to_manager_result(mgr_data, ticker)
        decision, confidence = final.decision, final.confidence
        summary, bull_case, bear_case = (
            final.summary,
            final.bull_case,
            final.bear_case,
        )
        would_upgrade_if, would_downgrade_if = (
            final.would_upgrade_if,
            final.would_downgrade_if,
        )
        if incognito:
            logger.info("[manager] incognito call made at %.0f%% confidence", confidence * 100)
        else:
            logger.info("[manager] %s %s %.0f%%", ticker, decision, confidence * 100)
    else:
        decision, confidence = "HOLD", 0.0
        summary = "Portfolio manager failed - defaulting to HOLD with no conviction."
        bull_case = bull_r.summary if bull_r else ""
        bear_case = bear_r.summary if bear_r else ""
        would_upgrade_if, would_downgrade_if = "", ""
    # Preserve the manager's own call before the deterministic risk gate
    # overwrites `decision`/`confidence` (ROADMAP P0.1) so the UI can show
    # `Manager: BUY -> Final: HOLD` with the exact flag that caused it.
    # The probability of the frozen success event travels with it (P1.1);
    # the risk gate never touches it.
    manager_decision = final.decision if mgr_data else None
    manager_confidence = final.confidence if mgr_data else None
    manager_probability = final.probability_beat_spy if mgr_data else None
    manager_weight = final.target_weight_pct if mgr_data else None
    target_weight_pct: float | None = None

    # Risk gate: deterministic sizing, forecast check, and exposure caps.
    size_usd, risk_flags = None, []
    if mgr_data:
        # Only the researchers this depth actually requested are gate inputs;
        # one skipped by the profile (fundamental and sentiment at Pro) is a
        # roster choice, not a failed input - the rule quality.build applies.
        gate_inputs = tuple(
            result
            for key, result in (
                ("technical", tech),
                ("fundamental", fund),
                ("news", news_r),
                ("sentiment", sentiment_r),
            )
            if key in research
        )
        decision, confidence, size_usd, risk_flags = risk.evaluate(
            decision,
            confidence,
            ticker,
            analysts=gate_inputs,
            vol_ann_pct=vol_ann_pct,
            portfolio=portfolio_summ,
            forecast_change_pct=forecast_change_pct,
            incognito=incognito,
            horizon_days=run_horizon,
        )
        # Weight contract (item 11): the manager's proposed share of equity
        # can only shrink the deterministic size, never raise it - the gate
        # stays the upper bound. The final weight is the actual position the
        # order path will size to (weight x equity at execution time).
        equity = portfolio_summ.total_equity if portfolio_summ else None
        if decision == "BUY" and size_usd and equity:
            size_usd, weight_flag = risk.cap_size_by_weight(
                size_usd, manager_weight, equity
            )
            if weight_flag:
                risk_flags.append(weight_flag)
            target_weight_pct = round(size_usd / equity * 100, 2)
        elif decision == "BUY" and manager_weight is not None:
            # No portfolio to convert dollars with (incognito / replay): the
            # gate's position cap still bounds the proposed weight.
            target_weight_pct = round(
                min(manager_weight, settings.max_position_pct * 100), 2
            )
        elif decision == "SELL":
            # A SELL exits the whole position: the target weight is zero.
            target_weight_pct = 0.0
    else:
        risk_flags = ["portfolio manager failed - defaulted to HOLD"]

    # Concentration check (ROADMAP P1.3): a surviving BUY with portfolio
    # context warns when correlated holdings behave like one concentrated
    # bet. Warning-only, and skipped when the caps could not run at all
    # (their own flag already says the portfolio was unavailable).
    concentration = None
    if (
        size_usd is not None
        and portfolio_summ is not None
        and portfolio_summ.total_equity
    ):
        if held_closes is None and portfolio_summ.positions:
            held_closes = await fetch_held_closes(portfolio_summ)
        concentration = risk.concentration_check(
            ticker,
            size_usd,
            portfolio_summ,
            held_closes=held_closes,
            candidate_closes=market.closes,
        )
        if concentration.status != "ok":
            prefix = (
                "concentration warning"
                if concentration.status == "high"
                else "concentration unknown"
            )
            risk_flags.append(f"{prefix}: {concentration.detail}")

    analysis = StockAnalysis(
        ticker=ticker,
        company_name=market.company_name,
        price=market.price,
        forecast_price_5d=indicators.get("forecast_price_5d"),
        forecast_change_5d_pct=indicators.get("forecast_change_5d_pct"),
        forecast_trend_r2=indicators.get("forecast_trend_r2"),
        forecast_method=indicators.get("forecast_method", ""),
        forecast_horizon_days=indicators.get("forecast_horizon_days"),
        forecast_path=indicators.get("forecast_path"),
        forecast_band_pct=None if forecast_band_pct is None else round(forecast_band_pct, 2),
        forecast_z=None if forecast_z_value is None else round(forecast_z_value, 2),
        decision=decision,
        confidence=confidence,
        manager_decision=manager_decision,
        manager_confidence=manager_confidence,
        manager_probability=manager_probability,
        manager_target_weight_pct=manager_weight,
        manager_votes=manager_votes,
        manager_agreement=manager_agreement,
        target_weight_pct=target_weight_pct,
        summary=summary,
        bull_case=bull_case,
        bear_case=bear_case,
        would_upgrade_if=would_upgrade_if,
        would_downgrade_if=would_downgrade_if,
        technical=tech,
        fundamental=fund,
        news=news_r,
        sentiment=sentiment_r,
        forecast=forecast_r,
        market=market_r,
        bull=bull_r,
        bear=bear_r,
        judge=judge_r,
        duration_s=round(time.perf_counter() - started, 1),
        stage_timings_s=stage_times,
        suggested_size_usd=size_usd,
        risk_flags=risk_flags,
        concentration=concentration,
        past_decisions=past["past_decisions"] if past else [],
        token_usage=token_totals,
        cost_estimate=cost.estimate(
            role_usage,
            settings.llm_price_in or None,
            settings.llm_price_out or None,
        ),
        as_of=market.as_of,
        providers={str(key): str(value) for key, value in market.sources.items()},
        source_references=_source_references(market),
        data_quality=quality.build(
            market.as_of,
            outlook,
            research,
            {
                "market": market_r,
                "technical": tech,
                "fundamental": fund,
                "news": news_r,
                "sentiment": sentiment_r,
                "forecast": forecast_r,
            },
            {str(key): str(value) for key, value in market.sources.items()},
        ),
    )
    logger.info(
        "[analysis] %s completed in %.1fs%s",
        "an incognito ticker" if incognito else ticker,
        analysis.duration_s,
        "" if incognito else f" ({decision})",
    )
    changes.attach(analysis, previous)
    # Record the decision for future reflection (ROADMAP 1.1) with its
    # calibration provenance (P1.1). A failure here must never surface to the
    # user or block the result. Backtest replays never write - they are not
    # live decisions; incognito runs never write either - they must leave no
    # trace. A manager output whose decision did not parse exactly
    # (P2.9) is not a real call either: it must not enter the track record as
    # a phantom HOLD.
    manager_parsed = mgr_data is None or manager.parse_decision(mgr_data) is not None
    if analysis.error is None and live_context and not incognito and manager_parsed:
        try:
            from app import calibration
            from app import memory

            await memory.record_decision(
                run_id,
                analysis,
                provenance={
                    "outlook": outlook,
                    "depth": depth,
                    "horizon_days": run_horizon,
                    "model": settings.llm_for("manager")["model"]
                    if settings.llm_configured
                    else "mock",
                    "policy_version": calibration.DECISION_POLICY_VERSION,
                    "success_event": calibration.success_event(run_horizon),
                },
            )
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
