"""Verify prospective daily inputs and metered Luna/Fast with no network or real ledger."""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
from importlib.util import find_spec
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

scratch = TemporaryDirectory(ignore_cleanup_errors=True)
root = Path(scratch.name)
os.environ.update(DB_PATH=str(root / "initial.db"), BTA_KEYRING_SERVICE=f"BTA-prospective-{root.name}",
                  LLM_API_KEY="", OWNER_TOKEN="", ALPACA_API_KEY_ID="", ALPACA_API_SECRET_KEY="")

import httpx  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from app.backtest.options import prospective_signals as study  # noqa: E402
from app.backtest.options.domain import CHICAGO, TICKERS  # noqa: E402

if find_spec("app.spending") is None:
    try:
        study._runtime()
    except study.SignalStudyError:
        print("Prospective Luna/Fast correctly blocks inference without the installed shared spending guard")
        raise SystemExit(0) from None
    raise AssertionError("Missing shared spending guard did not block inference")

from app import broker, memory, spending, workflow  # noqa: E402


ENTRY = datetime(2026, 11, 24, 8, 40, tzinfo=CHICAGO)
DEADLINE = ENTRY - timedelta(minutes=5)
NOW = ENTRY - timedelta(minutes=20)
CUTOFF, _, SESSIONS = study.input_window(ENTRY, DEADLINE)
TOKEN = "scratch-luna-key"


def bars():
    return [{"session": session.isoformat(), "open": 100 + i, "low": 99 + i, "high": 102 + i,
             "close": 101 + i, "volume": 1000 + i} for i, session in enumerate(SESSIONS)]


def fetcher(ticker, start, end):
    assert ticker in TICKERS and start == SESSIONS[0] and end == ENTRY.date()
    return bars()


def reply(request, *, missing_usage=False):
    assert str(request.url) == spending.OFFICIAL_BASE + "/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == spending.MODEL and body["service_tier"] == "default"
    ticker = study.ACTIVE_TICKER.get()
    result = {"ticker": ticker, "decision": "BUY", "signal": "bullish", "confidence": 0.8, "summary": "Offline"}
    payload = {"id": "offline-completion", "object": "chat.completion", "created": 1, "model": spending.MODEL,
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(result)}}]}
    if not missing_usage:
        payload["usage"] = {"prompt_tokens": 210, "completion_tokens": 50, "total_tokens": 260}
    return httpx.Response(200, json=payload, headers={"x-request-id": "offline-provider"})


def forbidden(*args, **kwargs):
    raise AssertionError("Private context, broker access, hosted forecasting, or extra I/O was attempted")


async def checks():
    assert CUTOFF == datetime(2026, 11, 23, 15, 0, tzinfo=CHICAGO)
    with ExitStack() as stack:
        stack.enter_context(patch.object(workflow.settings, "db_path", root / "meter.db"))
        stack.enter_context(patch.object(workflow.settings, "llm_model", study.MODEL))
        stack.enter_context(patch.object(workflow.settings, "llm_base_url", spending.OFFICIAL_BASE))
        stack.enter_context(patch.object(workflow.settings, "llm_api_key", TOKEN))
        stack.enter_context(patch.object(study, "engine_identity", return_value={"source": "offline-fixture"}))
        for target, name in ((workflow, "fetch_portfolio_summary"), (workflow, "fetch_past_decisions"),
                             (workflow, "get_hosted_forecast"), (workflow, "get_stock_data"),
                             (memory, "get_reflections"), (memory, "record_decision"), (broker, "submit_order")):
            stack.enter_context(patch.object(target, name, side_effect=forbidden))

        sends = []

        def mock_openai(**kwargs):
            def respond(request):
                sends.append(request)
                return reply(request, missing_usage=omit_usage and study.ACTIVE_TICKER.get() == "AAPL")

            return AsyncOpenAI(http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), **kwargs)

        stack.enter_context(patch("openai.AsyncOpenAI", side_effect=mock_openai))
        # Distinguish the manager's raw BUY from the final risk-gated decision.
        stack.enter_context(patch.object(workflow.risk, "evaluate", return_value=("HOLD", 0.5, 500.0, ["offline-risk-veto"])))
        prepared = await study.prepare_inputs(entry_at=ENTRY, deadline=DEADLINE, clock=lambda: NOW, fetcher=fetcher)
        assert set(prepared["inputs"]) == set(TICKERS) and not sends
        assert prepared["input_cutoff"] == CUTOFF.isoformat() and TOKEN not in json.dumps(prepared)
        assert all(row["bars"][-1]["session"] == "2026-11-23" for row in prepared["inputs"].values())

        def append_partial(ticker, start, end):
            return [*fetcher(ticker, start, end), {**bars()[-1], "session": ENTRY.date().isoformat()}]

        future = await study.prepare_inputs(entry_at=ENTRY, deadline=DEADLINE, clock=lambda: NOW, fetcher=append_partial)
        assert {row["failure_reason"] for row in future["inputs"].values()} == {"future_daily_bar"}
        assert study._snapshot(bars()[-84:], sessions=SESSIONS)["failure_reason"] == "insufficient_completed_daily_history"
        failure = await study.freeze_signals(future, cohort_id="future-bars", entry_at=ENTRY, deadline=DEADLINE, clock=lambda: NOW)
        assert failure["status"] == "incomplete" and len(failure["records"]) == 6 and not sends
        late = await study.freeze_signals(prepared, cohort_id="missed", entry_at=ENTRY, deadline=DEADLINE, clock=lambda: DEADLINE)
        assert late["status"] == "missed" and len(late["records"]) == 6 and not sends
        with patch.object(workflow.settings, "llm_api_key", ""):
            missing = await study.freeze_signals(prepared, cohort_id="missing-key", entry_at=ENTRY, deadline=DEADLINE, clock=lambda: NOW)
        assert missing["archive"] is None and len(missing["records"]) == 6 and not sends

        omit_usage = False
        complete = await study.freeze_signals(prepared, cohort_id="offline", entry_at=ENTRY, deadline=DEADLINE, clock=lambda: NOW)
        assert complete["status"] == "signals_frozen", complete
        archive = complete["archive"]
        assert archive is not None and archive.source == "imported_timestamped"
        for signal, record in zip(archive.signals, complete["records"]):
            assert signal.fast == "HOLD" and record["raw_manager"]["decision"] == "BUY"
            assert record["risk_flags"] == ["offline-risk-veto"] and record["forecast_method"] == "ridge_lag"
            assert signal.research_cost > 0 and record["momentum"] == signal.momentum
            assert "suggested_size_usd" not in record
            assert {request["returned_model"] for request in record["model_requests"]} == {study.MODEL}
            assert all(request["prompt_sha256"] == study._digest(request["messages"]) for request in record["model_requests"])
        assert TOKEN not in archive.model_dump_json() and len(sends) == 18
        assert (await study.recover_costs("offline"))["estimated_usd"] == complete["costs"]["estimated_usd"] > 0
        assert spending.Ledger().status()["limit_usd"] == 14

        omit_usage = True
        failed = await study.freeze_signals(prepared, cohort_id="bad-usage", entry_at=ENTRY, deadline=DEADLINE, clock=lambda: NOW)
        assert failed["archive"] is None and failed["status"] == "incomplete"
        assert len(failed["records"]) == 6 and failed["costs"]["reserved_usd"] > 0
        assert next(row for row in failed["records"] if row["ticker"] == "AAPL")["fast"] == "FAILED"


if __name__ == "__main__":
    asyncio.run(checks())
    print("Prospective Luna/Fast signal freeze, guarded fixture, cutoff and isolation checks passed")
