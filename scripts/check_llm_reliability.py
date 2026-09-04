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

# ---- one LLM object per role, JSON mode on by default -------------------------
mgr, ana, deb = workflow.get_llm("manager"), workflow.get_llm("analysts"), workflow.get_llm("debate")
assert mgr is not None and ana is not None and deb is not None
assert mgr is not ana and mgr is not deb, "roles must not share LLM objects"
assert mgr.model.endswith("zai-org/GLM-5.3") and ana.model.endswith("fast-model")
assert mgr.base_url.endswith("manager.example/v1")
assert mgr.additional_params.get("response_format") == {"type": "json_object"}
assert ana.additional_params.get("response_format") == {"type": "json_object"}
print("per-role LLMs OK:", mgr.model, "/", ana.model)

# ---- failure classification ----------------------------------------------------
assert workflow._classify_failure(ValueError("no JSON object in model output: 'BUY because'")) == "bad_output"
assert workflow._classify_failure(RuntimeError("HTTP 429 - rate limit exceeded")) == "rate_limit"
assert workflow._classify_failure(RuntimeError("server error 503 service unavailable")) == "server"
assert workflow._classify_failure(TimeoutError()) == "server"
assert workflow._classify_failure(RuntimeError("response_format is not supported")) == "response_format"
assert workflow._classify_failure(RuntimeError("connection reset by peer")) == "other"
print("failure classification OK")

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
