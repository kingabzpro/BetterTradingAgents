"""Offline checks for LLM reliability + per-role models (ROADMAP 2.3).

Run: PYTHONPATH=. uv run python scripts/check_llm_reliability.py
No network: crews are stubbed; only LLM *objects* are built. The final e2e
runs in mock mode (real yfinance data, like the other check scripts).
"""

import asyncio
import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "reliability_test.db")
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_MODEL"] = "fast-model"
os.environ["LLM_MODEL_MANAGER"] = "zai-org/GLM-5.3"
os.environ["LLM_BASE_URL_MANAGER"] = "https://manager.example/v1"
# A real .env may carry per-role overrides; blank the ones under test so the
# fallback resolution asserted below is actually exercised.
os.environ["LLM_MODEL_ANALYSTS"] = ""
os.environ["LLM_BASE_URL_ANALYSTS"] = ""
os.environ["LLM_MODEL_DEBATE"] = ""
os.environ["LLM_BASE_URL_DEBATE"] = ""
os.environ["LLM_API_KEY_MANAGER"] = ""
os.environ["LLM_API_KEY_ANALYSTS"] = ""
os.environ["LLM_API_KEY_DEBATE"] = ""
# Per-agent overrides: blank each one a real .env might carry, then set a
# single agent override so the agent -> role -> global chain is exercised.
for _agent in ("market", "technical", "fundamental", "news", "sentiment", "forecast", "judge"):
    for _part in ("MODEL", "BASE_URL", "API_KEY"):
        os.environ[f"LLM_{_part}_{_agent.upper()}"] = ""
os.environ["LLM_MODEL_TECHNICAL"] = "tech-model"

from app import workflow  # noqa: E402
from app.config import settings  # noqa: E402

# ---- per-role config resolution ----------------------------------------------
conf = settings.llm_for("manager")
assert conf["model"] == "zai-org/GLM-5.3" and conf["base_url"].endswith(
    "manager.example/v1"
), conf
assert conf["api_key"] == "test-key"  # falls back to the global key
assert settings.llm_for("analysts") == {
    "model": "fast-model",
    "base_url": settings.llm_base_url,
    "api_key": "test-key",
}
assert settings.llm_for("debate")["model"] == "fast-model"
print("per-role config OK: manager=GLM-5.3, analysts/debate=fast-model")

# ---- per-agent config resolution: agent -> role -> global ----------------------
assert settings.llm_for("technical") == {
    "model": "tech-model",  # the agent override wins
    "base_url": settings.llm_base_url,  # agent blank -> role blank -> global
    "api_key": "test-key",
}
assert settings.llm_key_for("technical") == "technical"  # own LLM object
assert settings.llm_key_for("fundamental") == "analysts"  # unoverridden: role LLM
assert settings.llm_key_for("bull") == "debate" and settings.llm_key_for("manager") == "manager"
# With only the global LLM set, every other agent resolves to it.
for _agent in ("market", "fundamental", "news", "sentiment", "forecast", "judge", "bull", "bear"):
    assert settings.llm_for(_agent) == {
        "model": "fast-model", "base_url": settings.llm_base_url, "api_key": "test-key"
    }, _agent
# A role override feeds its agents; an agent override still beats the role.
settings.llm_model_analysts = "analysts-model"
try:
    assert settings.llm_for("market")["model"] == "analysts-model"
    assert settings.llm_for("technical")["model"] == "tech-model"
    assert settings.llm_key_for("market") == "analysts"
finally:
    settings.llm_model_analysts = ""
print("per-agent config OK: technical=tech-model, rest follow role/global")

# ---- one LLM object per role, JSON mode on by default -------------------------
mgr, ana, deb = workflow.get_llm("manager"), workflow.get_llm("analysts"), workflow.get_llm("debate")
assert mgr is not None and ana is not None and deb is not None
assert mgr is not ana and mgr is not deb, "roles must not share LLM objects"
assert mgr.model.endswith("zai-org/GLM-5.3") and ana.model.endswith("fast-model")
assert mgr.base_url.endswith("manager.example/v1")
assert mgr.additional_params.get("response_format") == {"type": "json_object"}
assert ana.additional_params.get("response_format") == {"type": "json_object"}
print("per-role LLMs OK:", mgr.model, "/", ana.model)

# ---- P2.18: runaway bounds forwarded to every provider ----------------------
settings.llm_max_tokens = 4096
capped = workflow._build_llm("analysts")
assert capped.max_tokens == 4096, capped.max_tokens
settings.llm_max_tokens = 0
uncapped = workflow._build_llm("analysts")
assert uncapped.max_tokens is None, uncapped.max_tokens  # 0 = today's behaviour
print("max_tokens OK: 4096 forwarded, 0 leaves the provider default")

# ---- failure classification ----------------------------------------------------
assert workflow._classify_failure(ValueError("no JSON object in model output: 'BUY because'")) == "bad_output"
assert workflow._classify_failure(RuntimeError("HTTP 429 - rate limit exceeded")) == "rate_limit"
assert workflow._classify_failure(RuntimeError("server error 503 service unavailable")) == "server"
assert workflow._classify_failure(TimeoutError()) == "server"
assert workflow._classify_failure(RuntimeError("response_format is not supported")) == "response_format"
assert workflow._classify_failure(RuntimeError("connection reset by peer")) == "other"
print("failure classification OK")

# ---- provider compatibility: agent calls must not force json_schema ----
# structured output (output_type) makes the SDK replace our json_object with a
# strict json_schema request; providers like api.deepseek.com reject that with
# a 400 on every call.
from app.agents import (  # noqa: E402
    bear,
    bull,
    forecast,
    fundamental,
    judge,
    manager,
    news,
    sentiment,
    technical,
)
from app.agents import market as market_agent  # noqa: E402

_llm = workflow.get_llm("analysts")
_AGENT_TASKS = (
    (market_agent, {}), (technical, {}), (fundamental, {}), (news, {"items": []}),
    (sentiment, {"items": []}), (forecast, {}), (bull, {}), (bear, {}),
    (judge, {"research": {}, "bull_round_1": {}, "bear_round_1": {}}), (manager, {}),
)
for mod, payload in _AGENT_TASKS:
    task = mod.build_task(mod.build_agent(_llm), "NVDA", payload)
    assert getattr(task, "output_pydantic", None) is None, mod.NAME
print("task builders OK: no output_pydantic, JSON mode stays json_object")

# ---- a total agent wipeout fails the run instead of a fake HOLD -----------
from app.models import AgentResult, StockAnalysis  # noqa: E402
from app.runs import _no_usable_output  # noqa: E402

assert _no_usable_output(StockAnalysis(ticker="X"))  # every agent dead
assert _no_usable_output(StockAnalysis(ticker="X", error="boom"))  # ticker crashed
assert not _no_usable_output(StockAnalysis(ticker="X", manager_decision="HOLD"))
assert not _no_usable_output(StockAnalysis(
    ticker="X",
    technical=AgentResult(agent="technical", signal="neutral", confidence=0.5),
))
print("run-failure rule OK: all-agents-dead marks the run failed")

# ---- stubbed crews for the retry paths -----------------------------------------


class FakeTask:
    def __init__(self, ticker):
        self.description = f"Make the call for {ticker}."


class FakeMod:
    NAME = "manager"

    def build_agent(self, llm):
        return object()

    def build_task(self, agent, ticker, **payload):
        return FakeTask(ticker)

    def mock(self, ticker, **payload):
        return {}


events = []


async def emit(kind, payload):
    events.append((kind, payload))


async def run_retry_test(first_error, expect_calls, expect_correction):
    calls = []

    async def fake_kick(agent, task):
        calls.append(task.description)
        if len(calls) == 1 and first_error is not None:
            raise first_error
        return ({"decision": "BUY", "confidence": 0.9, "summary": "ok"},
                {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 0, "total_tokens": 15})

    workflow._kick_once = fake_kick
    totals: dict = {}
    data = await workflow._run_agent(FakeMod(), "NVDA", emit, token_totals=totals)
    assert data is not None and data["decision"] == "BUY", data
    assert len(calls) == expect_calls, calls
    if expect_correction:
        assert calls[1] != calls[0] and "rejected" in calls[1], calls[1][:120]
    else:
        assert calls[1] == calls[0], "non-output failures must retry the same task"
    assert totals.get("total_tokens") == 15, totals
    completed = [p for kind, p in events if kind == "agent_completed"]
    assert completed and completed[-1].get("tokens") == 15
    return totals


async def retry_checks():
    workflow._BACKOFF_SECONDS = (0.0, 0.0)  # keep the backoff test instant
    events.clear()

    await run_retry_test(ValueError("no JSON object in model output: 'all in on NVDA'"), 2, True)
    print("corrective retry OK: malformed output retried with the error fed back")

    await run_retry_test(RuntimeError("HTTP 429 - rate limit exceeded"), 2, False)
    print("rate-limit retry OK: same task retried after backoff")

    await run_retry_test(RuntimeError("server error 503 - overloaded"), 2, False)
    print("server-error retry OK")

    # P2.18: retries=0 fails fast - a rate limit gets no second attempt
    settings.llm_max_retries = 0
    calls = []

    async def always_429(agent, task):  # noqa: ARG001
        calls.append(task.description)
        raise RuntimeError("HTTP 429 - rate limit exceeded")

    workflow._kick_once = always_429
    data = await workflow._run_agent(FakeMod(), "NVDA", emit, token_totals={})
    assert data is None and len(calls) == 1, calls
    assert events[-1][0] == "agent_failed"
    settings.llm_max_retries = 2
    print("retry budget OK: retries=0 fails fast on a rate limit")

    # JSON mode self-healing: role LLM rebuilt without response_format
    workflow._llms["manager"] = workflow._build_llm("manager")  # ensure it's on
    await run_retry_test(RuntimeError("response_format is not supported by this provider"), 2, False)
    assert "response_format" not in (workflow._llms["manager"].additional_params or {})
    print("JSON-mode self-heal OK: response_format dropped after rejection")

    # unknown failure: no retry, the slot degrades to None
    calls = []

    async def always_failing(agent, task):
        calls.append(task.description)
        raise RuntimeError("connection reset by peer")

    workflow._kick_once = always_failing
    data = await workflow._run_agent(FakeMod(), "NVDA", emit, token_totals={})
    assert data is None and len(calls) == 1
    assert events[-1][0] == "agent_failed"
    print("unknown-failure degradation OK: no blind retry, agent_failed emitted")

    # ---- LLM_CONCURRENCY: the app-wide gate caps in-flight agent calls ------
    # Shared endpoints (e.g. Modal's, 16 concurrent per workspace) bounce
    # bursts; the gate keeps the app under the ceiling and rebuilds live when
    # the setting changes.
    overlap = {"now": 0, "max": 0}

    async def slow_kick(agent, task):  # noqa: ARG001
        overlap["now"] += 1
        overlap["max"] = max(overlap["max"], overlap["now"])
        await asyncio.sleep(0.05)
        overlap["now"] -= 1
        return ({"decision": "BUY", "confidence": 0.9, "summary": "ok"},
                {"total_tokens": 1})

    workflow._kick_once = slow_kick
    settings.llm_concurrency = 1
    await asyncio.gather(*[
        workflow._run_agent(FakeMod(), "NVDA", emit) for _ in range(3)
    ])
    assert overlap["max"] == 1, overlap  # gate serialized the three calls
    settings.llm_concurrency = 0
    overlap = {"now": 0, "max": 0}
    await asyncio.gather(*[
        workflow._run_agent(FakeMod(), "NVDA", emit) for _ in range(3)
    ])
    assert overlap["max"] == 3, overlap  # 0 = unlimited: all three at once
    settings.llm_concurrency = 12
    print("LLM_CONCURRENCY gate OK: 1 serializes, 0 runs unlimited")


asyncio.run(retry_checks())

# ---- mock e2e: the whole pipeline still works, token_usage empty ---------------
settings.llm_api_key = ""
workflow._llms.clear()
workflow._llm_roles_initialized.clear()

from app.workflow import analyze_ticker  # noqa: E402


async def e2e():
    result = await asyncio.wait_for(analyze_ticker("NVDA", emit), timeout=120)
    assert result.error is None, result.error
    assert result.token_usage == {}, result.token_usage
    print("mock e2e OK:", result.decision, "| token_usage empty in mock mode")


asyncio.run(e2e())
print("ALL LLM RELIABILITY CHECKS PASSED")
