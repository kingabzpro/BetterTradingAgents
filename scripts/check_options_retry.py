"""Verify the options API simulation boundary and durable replay receipts."""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

scratch = TemporaryDirectory(ignore_cleanup_errors=True)
root = Path(scratch.name)
os.environ.update(DB_PATH=str(root / "options-retry.db"), BTA_KEYRING_SERVICE=f"BTA-test-{root.name}",
                  OWNER_TOKEN="", LLM_API_KEY="", ALPACA_API_KEY_ID="", ALPACA_API_SECRET_KEY="",
                  ALPACA_API_KEY="", ALPACA_SECRET_KEY="", OPENAI_API_KEY="", AUTOMATION_ENABLED="0")

from app.backtest.options import api  # noqa: E402
from app.backtest.options.domain import EXECUTION_POLICY_VERSION  # noqa: E402
from app.backtest.options.jobs import Jobs  # noqa: E402
from app.backtest.options.replay import replay  # noqa: E402
from app.main import app  # noqa: E402

RUNS = "/api/backtests/runs"
REQUEST = {"mode": "options_intraday_v1", "preset": "intraday_7_14_dte", "source": "synthetic",
           "start": "2025-06-02", "end": "2025-06-02"}


async def check_replay_requests(client, saved_root, executed):
    headers = {"Idempotency-Key": str(uuid4())}
    barrier = Barrier(2)
    prepare = api.jobs.prepare

    def prepare_in_parallel(request):
        prepared = prepare(request)
        barrier.wait(timeout=10)
        return prepared

    with patch.object(api.jobs, "prepare", side_effect=prepare_in_parallel):
        one, two = await asyncio.gather(*(client.post(RUNS, json=REQUEST, headers=headers) for _ in range(2)))
    assert one.status_code == two.status_code == 202, (one.text, two.text)
    run_id = one.json()["run_id"]
    assert two.json()["run_id"] == run_id
    if active := api.jobs.active.get(run_id):
        await asyncio.wait_for(asyncio.shield(active[0]), timeout=10)
    result = (await client.get(f"{RUNS}/{run_id}")).json()
    assert result["status"] == "complete", result
    assert result["result"]["manifest"]["account_mode"] == "simulated"
    assert result["result"]["manifest"]["execution_policy_version"] == EXECUTION_POLICY_VERSION
    assert result["result"]["primary"]["accounts"]["fast"]["positions_opened"] > 0
    assert executed.call_count == 1
    status_path = api.jobs.find(run_id)
    original = status_path.read_bytes()
    with patch.object(Path, "read_bytes", side_effect=[PermissionError("simulated Windows sharing violation"), original]):
        assert api.jobs._status(status_path)["status"] == "complete"

    api.jobs = Jobs(saved_root)
    retry = await client.post(RUNS, json=REQUEST, headers=headers)
    assert retry.status_code == 202 and retry.json()["run_id"] == run_id
    assert (await client.get(f"{RUNS}/{run_id}")).json() == result
    conflict = await client.post(RUNS, json={**REQUEST, "end": "2025-06-03"}, headers=headers)
    assert conflict.status_code == 409 and "already used" in conflict.json()["detail"]
    assert executed.call_count == 1


async def check_interrupted_claim(client, saved_root, executed):
    headers = {"Idempotency-Key": str(uuid4())}
    with patch("app.backtest.options.jobs._write", side_effect=OSError("simulated crash before writing status")):
        response = await client.post(RUNS, json=REQUEST, headers=headers)
    assert response.status_code == 500, response.text

    api.jobs = Jobs(saved_root)
    retry = await client.post(RUNS, json=REQUEST, headers=headers)
    assert retry.status_code == 202 and retry.json()["status"] == "interrupted", retry.text
    run_id = retry.json()["run_id"]
    assert (await client.get(f"{RUNS}/{run_id}")).json() == retry.json()
    assert any(row["run_id"] == run_id and row["status"] == "interrupted" for row in (await client.get(RUNS)).json())
    assert (await client.get(f"{RUNS}/{run_id}/export")).status_code == 409
    assert executed.call_count == 1


async def check():
    saved_root = root / "runs"
    api.jobs = Jobs(saved_root)
    transport = ASGITransport(app=app, client=("127.0.0.1", 42690), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        with patch("app.broker._client", side_effect=AssertionError("options reached a broker")) as broker_client, \
             patch("app.broker.submit_order", side_effect=AssertionError("options submitted an order")) as submit, \
             patch("app.backtest.options.jobs.replay", wraps=replay) as executed:
            headers = {"Idempotency-Key": str(uuid4())}
            for override in ({"account_mode": "live"}, {"source": "live"}, {"execution": {"broker": "live"}}):
                rejected = await client.post(RUNS, json={**REQUEST, **override}, headers=headers)
                assert rejected.status_code == 422, rejected.text
            missing = await client.post(RUNS, json=REQUEST)
            assert missing.status_code == 422 and "Idempotency-Key" in missing.json()["detail"]
            malformed = await client.post(RUNS, json=REQUEST, headers={"Idempotency-Key": "not-a-uuid"})
            assert malformed.status_code == 422 and not executed.called
            await check_replay_requests(client, saved_root, executed)
            await check_interrupted_claim(client, saved_root, executed)
            broker_client.assert_not_called()
            submit.assert_not_called()


if __name__ == "__main__":
    asyncio.run(check())
    print("Options simulation, concurrent retry and restart checks passed")
