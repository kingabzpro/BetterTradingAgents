"""Offline checks for the per-ticker portfolio-manager chat.

Run: PYTHONPATH=. uv run python scripts/check_chat.py
No network: mock mode (no LLM key), an in-memory MarketData snapshot drives
the pipeline, and the API is exercised through httpx's ASGI transport.
"""

import asyncio
import os
import random
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "chat_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""

import httpx  # noqa: E402

from app import chat, memory, portfolio, run_history, workflow  # noqa: E402
from app.main import app  # noqa: E402
from app.models import StockAnalysis  # noqa: E402
from app.runs import Run, store  # noqa: E402
from app.tools.market_data import MarketData  # noqa: E402

# ---- the chat LLM is the manager's model, plain prose --------------------------
async def llm_checks():
    settings = workflow.settings
    settings.llm_api_key = "test-key"
    settings.llm_model = "strong-model"
    workflow._llms.clear()
    workflow._llm_roles_initialized.clear()
    llm = workflow.get_chat_llm()
    assert llm is not None
    assert getattr(llm, "stream", None) is False, "chat must not stream tokens"
    assert "response_format" not in (llm.additional_params or {}), "chat must not force JSON"
    assert workflow.get_llm("manager") is not llm, "verdict LLM stays JSON-mode"
    settings.llm_api_key = ""
    workflow._llms.clear()
    workflow._llm_roles_initialized.clear()


asyncio.run(llm_checks())
print("chat LLM OK: manager model, no JSON mode, no streaming")

# ---- dossier + mock answer ------------------------------------------------------
analysis = StockAnalysis(
    ticker="NVDA", company_name="Nvidia Corp", price=120.5, decision="BUY",
    confidence=0.72, summary="Bull case outweighs bear case.",
)
dossier = chat._dossier(analysis, None)
assert dossier["system_view"]["decision"] == "BUY"
assert dossier["current_portfolio"] == "UNAVAILABLE - portfolio lookup failed"
assert dossier["analysts"]["technical"] is None and dossier["forecast_5d"]["z"] is None
answer = chat.mock_answer(analysis)
assert "NVDA" in answer and "BUY" in answer and "[mock]" in answer
print("dossier + mock answer OK")

# ---- hermetic e2e: mock pipeline -> per-ticker chat over the API ---------------
def snapshot(ticker: str) -> MarketData:
    random.seed(41)
    closes = [100.0]
    for _ in range(129):
        closes.append(closes[-1] * (1 + random.uniform(-0.015, 0.02)))
    return MarketData(
        ticker=ticker,
        price=closes[-1],
        company_name="Nvidia Corp",
        closes=closes,
        highs=[c * 1.01 for c in closes],
        lows=[c * 0.99 for c in closes],
        volumes=[1_000_000.0] * len(closes),
        fundamentals={"pe_ratio_ttm": 35.0},
        news=[{"title": "Guidance raised", "source": "finnhub", "summary": "beat",
               "url": "", "published": ""}],
        social=[],
        sources={"prices": "yfinance", "fundamentals": "none",
                 "news": "none", "social": "none"},
        as_of="2026-09-05T20:00:00+00:00",
    )


async def e2e():
    await portfolio.init()
    await memory.init()
    await run_history.init()

    async def emit(kind, payload):  # noqa: ARG001 - events are not under test here
        pass

    result = await asyncio.wait_for(
        workflow.analyze_ticker("NVDA", emit, market_data=snapshot("NVDA"),
                                live_context=True),
        timeout=90,
    )
    assert result.error is None, result.error

    run = Run(["NVDA", "AMD"])
    # A finished ticker is chattable while the run is still going.
    run.results["NVDA"] = result
    run.results["AMD"] = StockAnalysis(ticker="AMD", error="market data failed")
    store.runs[run.run_id] = run

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/runs/{run.run_id}/chat", json={
            "ticker": "nvda",
            "messages": [{"role": "user",
                          "content": "Should I invest in this apart from my portfolio?"}],
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ticker"] == "NVDA" and body["mock_mode"] is True
        assert "NVDA" in body["answer"] and result.decision in body["answer"]

        response = await client.post(f"/api/runs/{run.run_id}/chat", json={
            "ticker": "NVDA",
            "messages": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "and the risks?"},
            ],
        })
        assert response.status_code == 200 and response.json()["answer"]
        print("e2e OK: finished ticker chattable mid-run, multi-turn history accepted")

        assert (await client.post(f"/api/runs/{run.run_id}/chat", json={
            "ticker": "AMD",
            "messages": [{"role": "user", "content": "anything"}],
        })).status_code == 409
        assert (await client.post(f"/api/runs/{run.run_id}/chat", json={
            "ticker": "MSFT",
            "messages": [{"role": "user", "content": "anything"}],
        })).status_code == 404
        assert (await client.post(f"/api/runs/{run.run_id}/chat", json={
            "ticker": "nvda!!",
            "messages": [{"role": "user", "content": "anything"}],
        })).status_code == 400
        assert (await client.post(f"/api/runs/{run.run_id}/chat", json={
            "ticker": "NVDA",
            "messages": [{"role": "assistant", "content": "must end with the user"}],
        })).status_code == 400
        assert (await client.post(f"/api/runs/{run.run_id}/chat", json={
            "ticker": "NVDA",
            "messages": [{"role": "user", "content": "x" * 2001}],
        })).status_code == 422
        assert (await client.post("/api/runs/doesnotexist/chat", json={
            "ticker": "NVDA",
            "messages": [{"role": "user", "content": "anything"}],
        })).status_code == 404
        print("validation OK: 409 failed ticker, 404 missing, 400 bad input, 422 oversized")

    # After a restart (in-memory run gone), the SQLite fallback still answers.
    run.status = "completed"
    await run_history.save(run.to_status(), owner_id="chat_check")
    store.runs.pop(run.run_id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/runs/{run.run_id}/chat", json={
            "ticker": "NVDA",
            "messages": [{"role": "user", "content": "still there?"}],
        })
        assert response.status_code == 200 and response.json()["answer"]
    print("history fallback OK: restored run stays chattable")


asyncio.run(e2e())
print("ALL CHAT CHECKS PASSED")
