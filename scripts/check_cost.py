"""Offline checks for the per-run estimated model cost (ROADMAP P2.2 slice).

Run: PYTHONPATH=. uv run python scripts/check_cost.py
No network: price lookups and estimate math are pure functions; persistence
and the API are exercised through SQLite and httpx's ASGI transport.
"""

import asyncio
import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "cost_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""

import httpx  # noqa: E402

from app import cost, run_history  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models import RunStatus, StockAnalysis  # noqa: E402

# ---- price table lookups --------------------------------------------------------
assert cost.price_for("zai-org/GLM-5.3") == (1.40, 4.40)
assert cost.price_for("glm-5.3-flash") == (0.15, 0.50)
assert cost.price_for("openai/gpt-5.6-luna") == (0.20, 1.20)
assert cost.price_for("GPT-4o-mini") == (0.15, 0.60)
assert cost.price_for("gpt-4.1-mini") == (0.40, 1.60)
assert cost.price_for("my-localhost-model") is None
assert cost.normalize_model("zai-org/GLM-5.3-Flash") == cost.normalize_model("glm5.3-flash")
print("price table OK: GLM-5.3/Flash, gpt-5.6-luna, gpt-4o-mini; unknown -> None")

# ---- estimate math ---------------------------------------------------------------
estimate = cost.estimate({
    "manager": {"model": "zai-org/GLM-5.3", "prompt_tokens": 1_000_000, "completion_tokens": 500_000, "total_tokens": 1_500_000},
    "analysts": {"model": "GLM-5.3-Flash", "prompt_tokens": 2_000_000, "completion_tokens": 200_000, "total_tokens": 2_200_000},
})
assert estimate["priced"] is True
assert estimate["total_usd"] == 1.40 + 2.20 + 0.30 + 0.10  # 4.00 exactly
assert estimate["by_role"]["manager"]["usd"] == 3.60
assert estimate["by_role"]["analysts"]["usd"] == 0.40
assert estimate["unpriced_models"] == []
assert estimate["prices_as_of"] == cost.PRICES_AS_OF

floor = cost.estimate({
    "manager": {"model": "zai-org/GLM-5.3", "prompt_tokens": 1_000_000, "completion_tokens": 0},
    "debate": {"model": "proxy/unknown-model", "prompt_tokens": 500_000, "completion_tokens": 100_000},
})
assert floor["priced"] is False
assert floor["total_usd"] == 1.40  # the unpriced role contributes nothing
assert floor["unpriced_models"] == ["proxy/unknown-model"]  # raw configured name
assert floor["by_role"]["debate"]["usd"] is None

assert cost.estimate({}) == {}
assert cost.estimate({"manager": {"model": "gpt-5.6-luna", "prompt_tokens": 0, "completion_tokens": 0}}) == {}

override = cost.estimate(
    {"manager": {"model": "anything", "prompt_tokens": 1_000, "completion_tokens": 1_000}},
    price_in=1.0,
    price_out=2.0,
)
assert override["priced"] is True and override["total_usd"] == 0.003
assert settings.llm_price_in == 0.0 and settings.llm_price_out == 0.0  # table by default
print("estimate math OK: priced total, unpriced floor, empty, price override")

# ---- old runs still parse ----------------------------------------------------------
legacy = StockAnalysis.model_validate({"ticker": "AAPL", "token_usage": {"total_tokens": 10}})
assert legacy.cost_estimate == {}
print("backward compatibility OK: pre-cost runs parse with an empty estimate")


# ---- persistence + history aggregation + API ---------------------------------------
def priced_result(ticker: str) -> StockAnalysis:
    return StockAnalysis.model_validate({
        "ticker": ticker,
        "decision": "BUY",
        "token_usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000, "total_tokens": 1_500_000},
        "cost_estimate": cost.estimate({
            "manager": {"model": "zai-org/GLM-5.3", "prompt_tokens": 1_000_000, "completion_tokens": 500_000},
        }),
    })


def unpriced_result(ticker: str) -> StockAnalysis:
    # Tokens were recorded but the model never got an estimate (or had none):
    # the run total must become a floor, never silently zero-risk.
    return StockAnalysis.model_validate({
        "ticker": ticker,
        "decision": "HOLD",
        "token_usage": {"total_tokens": 42},
    })


async def checks() -> None:
    await run_history.init()
    priced_run = RunStatus(
        run_id="priced00001",
        tickers=["NVDA"],
        status="completed",
        started_at=1_780_000_000,
        results={"NVDA": priced_result("NVDA")},
    )
    mixed_run = RunStatus(
        run_id="mixed000001",
        tickers=["AMD", "TSLA"],
        status="completed",
        started_at=1_780_000_100,
        results={"AMD": priced_result("AMD"), "TSLA": unpriced_result("TSLA")},
    )
    await run_history.save(priced_run, completed_at=1_780_000_010, owner_id="device_cost_test")
    await run_history.save(mixed_run, completed_at=1_780_000_110, owner_id="device_cost_test")

    restored = await run_history.get("priced00001")
    assert restored is not None
    assert restored.results["NVDA"].cost_estimate["total_usd"] == 3.60

    items = await run_history.list_runs("device_cost_test", 50)
    by_id = {item.run_id: item for item in items}
    assert by_id["priced00001"].cost_usd == 3.60 and by_id["priced00001"].cost_unknown is False
    assert by_id["mixed000001"].cost_usd == 3.60 and by_id["mixed000001"].cost_unknown is True

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get("/api/runs", headers={"X-Client-ID": "device_cost_test"})
        assert listing.status_code == 200
        rows = {row["run_id"]: row for row in listing.json()}
        assert rows["priced00001"]["cost_usd"] == 3.60
        assert rows["priced00001"]["cost_unknown"] is False
        assert rows["mixed000001"]["cost_unknown"] is True
        detail = await client.get("/api/runs/priced00001")
        assert detail.json()["results"]["NVDA"]["cost_estimate"]["by_role"]["manager"]["usd"] == 3.60


asyncio.run(checks())
print("ALL COST CHECKS PASSED")
