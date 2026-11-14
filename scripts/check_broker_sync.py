"""Offline checks for broker sync and failure hardening (ROADMAP P2.3).

Run: PYTHONPATH=. uv run python scripts/check_broker_sync.py
No network and no broker calls: the Alpaca client seam is replaced with a
fake, so every scenario is deterministic and key-less. Covers the startup
reconciliation sweep (pending/unknown rows settle honestly, partial fills
land), the drift check that backs the portfolio page's out-of-sync banner,
the stored sync state and its /api/broker/sync endpoint, and the classified
retries on broker read paths.
"""

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

# Isolated DB and cleared Alpaca env before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "broker_sync_test.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["BAKO_API_KEY"] = ""
os.environ["ALPACA_API_KEY_ID"] = ""
os.environ["ALPACA_API_SECRET_KEY"] = ""
os.environ["ALPACA_API_KEY"] = ""
os.environ["ALPACA_SECRET_KEY"] = ""
os.environ["ALPACA_TRADING_ENABLED"] = ""

import httpx  # noqa: E402
from alpaca.common.exceptions import APIError  # noqa: E402

from app import broker, run_history  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

FAKE_KEY = "PKCHECKKEY"
FAKE_SECRET = "secret-check-value-must-never-leak"

REAL_CLIENT = broker._client


def _use(fake):
    broker._client = lambda: fake


def _restore():
    broker._client = REAL_CLIENT


def _api_error(status: int, code: int, message: str) -> APIError:
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status))
    return APIError(json.dumps({"code": code, "message": message}), http_error)


def _remote_order(client_order_id: str, status: str = "filled", **fields):
    return SimpleNamespace(
        id=uuid4(),
        client_order_id=client_order_id,
        status=status,
        filled_qty=fields.get("filled_qty"),
        filled_avg_price=fields.get("filled_avg_price"),
        **{k: v for k, v in fields.items() if k not in ("filled_qty", "filled_avg_price")},
    )


def _raw_account():
    return SimpleNamespace(
        account_number="PA-TEST-123",
        status="ACTIVE",
        equity="100000.00",
        cash="50000.00",
        buying_power="200000.00",
        last_equity="100000.00",
        trading_blocked=False,
    )


class _FakeClient:
    """Quacks like TradingClient: by-id lookups, open orders, account reads."""

    def __init__(self, account=None):
        self.account = account
        self.by_client_id: dict[str, object] = {}
        self.errors_by_client_id: dict[str, Exception] = {}
        self.open_orders: list = []
        self.account_errors: list = []
        self.account_calls = 0

    def get_account(self):
        self.account_calls += 1
        if self.account_errors:
            raise self.account_errors.pop(0)
        return self.account

    def get_order_by_client_id(self, client_order_id: str):
        error = self.errors_by_client_id.get(client_order_id)
        if error is not None:
            raise error
        order = self.by_client_id[client_order_id]
        return order

    def get_orders(self, request_params):
        assert str(request_params.status) .lower().endswith("open"), "the drift check asks for OPEN orders"
        return self.open_orders


async def _insert(run_id: str, ticker: str, side: str) -> str:
    client_order_id = f"bta-{uuid4()}"
    await asyncio.to_thread(
        broker._insert_order, client_order_id, run_id, ticker, side, 10.0, 1000.0,
        "BUY" if side == "buy" else "SELL", 0.9,
        datetime.now(timezone.utc).isoformat(),
    )
    return client_order_id


async def checks() -> None:
    await run_history.init()
    await broker.init()

    # ---- unconfigured: everything is a no-op, the state stays pristine -----
    state = await broker.sync_state()
    assert state["checked_at"] is None and state["in_sync"] is True
    await broker.reconcile_startup()  # must not raise
    broker.start_sync_loop()  # must not spawn a sweep
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        body = (await client.get("/api/broker/sync")).json()
        assert body["checked_at"] is None and body["in_sync"] is True and body["drift"] == []
    print("unconfigured no-op OK (pristine state served to the banner)")

    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET

    # ---- never-traded install: a foreign open order is not OUR drift --------
    # Zero local order rows (this install never traded), one open order at
    # the broker placed by another surface sharing the account: the sweep
    # must stay quiet - no drift banner for users who never opted in here.
    foreign = _remote_order("bta-from-another-install", status="new")
    foreign.symbol = "ALAB"
    fresh_fake = _FakeClient(account=_raw_account())
    fresh_fake.open_orders = [foreign]
    _use(fresh_fake)
    state = await broker.sync_once()
    assert state["in_sync"] is True and state["drift"] == [], state
    print("never-traded OK (foreign open order on an empty history is not drift)")

    # ---- startup recovery: a crashed pending_submit row settles ------------
    filled_id = await _insert("syncrun01", "NVDA", "buy")
    unknown_id = await _insert("syncrun01", "TSLA", "buy")
    fake = _FakeClient(account=_raw_account())
    fake.by_client_id[filled_id] = _remote_order(
        filled_id, filled_qty="3", filled_avg_price="101.5"
    )
    fake.errors_by_client_id[unknown_id] = _api_error(404, 40410000, "order not found")
    _use(fake)
    await broker.reconcile_startup()
    row = await asyncio.to_thread(broker._get_order_row, filled_id)
    assert row["status"] == "filled" and row["filled_qty"] == 3.0, dict(row)
    assert row["filled_avg_price"] == 101.5
    row = await asyncio.to_thread(broker._get_order_row, unknown_id)
    assert row["status"] == "rejected" and "never saw" not in (row["error"] or "")
    print("startup recovery OK (pending settles filled; never-submitted settles rejected)")

    # ---- partial fill: refreshed into the row, still non-terminal ----------
    partial_id = await _insert("syncrun01", "AMD", "buy")
    fake.by_client_id[partial_id] = _remote_order(
        partial_id, status="partially_filled", filled_qty="1", filled_avg_price="99.0"
    )
    fake.open_orders = []
    state = await broker.sync_once()
    row = await asyncio.to_thread(broker._get_order_row, partial_id)
    assert row["status"] == "partially_filled" and row["filled_qty"] == 1.0
    assert state["reconciled"] >= 1 and state["in_sync"] is True and not state["drift"]
    print("partial fill sync OK (qty and price land, row stays open)")

    # ---- drift: an open order at the broker this app did not place ---------
    stray = _remote_order("bta-stray-order", status="new")
    stray.symbol = "GME"
    stray.side = SimpleNamespace(value="buy")
    stray.notional = "750.00"
    stray.submitted_at = "2026-10-14T13:00:00Z"
    fake.open_orders = [stray]
    state = await broker.sync_once()
    assert state["in_sync"] is False and len(state["drift"]) == 1, state
    assert state["drift"][0]["client_order_id"] == "bta-stray-order"
    assert state["drift"][0]["symbol"] == "GME" and state["drift"][0]["notional"] == 750.0
    stored = await broker.sync_state()
    assert stored["in_sync"] is False, "the drift must persist for the banner"
    print("drift check OK (stray open order flagged and stored)")

    # ---- back in sync -------------------------------------------------------
    fake.open_orders = []
    state = await broker.sync_once()
    assert state["in_sync"] is True and not state["drift"]
    print("re-sync OK (drift clears)")

    # ---- sweep cannot reach the broker: error recorded, no crash -----------
    fake.open_orders = None  # sentinel: make get_orders raise
    def _boom(request_params):
        raise _api_error(503, 50300000, "unavailable")
    fake.get_orders = _boom
    state = await broker.sync_once()
    assert state["error"] and "HTTP 503" in state["error"], state
    await broker.reconcile_startup()  # a failing sweep must not kill startup
    print("sweep failure OK (recorded as an error, startup survives)")

    # ---- classified retries on read paths ----------------------------------
    flaky = _FakeClient(account=_raw_account())
    flaky.account_errors = [_api_error(429, 42900000, "rate limit"), _api_error(500, 50000000, "boom")]
    broker._client = lambda: flaky
    account = await broker.account()
    assert account.equity == 100000.0 and flaky.account_calls == 3, flaky.account_calls
    fatal = _FakeClient(account=_raw_account())
    fatal.account_errors = [_api_error(401, 40100000, "bad key")]
    broker._client = lambda: fatal
    try:
        await broker.account()
        raise AssertionError("a 401 must fail immediately")
    except broker.BrokerRuleError as exc:
        assert exc.status_code == 503 and fatal.account_calls == 1
    print("classified retries OK (429+500 retried to success; 401 fails at once)")

    # ---- equity high-water mark (P2.13): ratchets up, holds on dips ---------
    def _account_with(equity: str):
        raw = _raw_account()
        raw.equity = equity
        return raw

    broker._client = lambda: _FakeClient(account=_raw_account())
    await broker.account()  # the first successful read seeds the mark
    mark = await broker.equity_mark()
    assert mark.high_water == 100000.0 and mark.note == "new equity high", mark
    broker._client = lambda: _FakeClient(account=_account_with("93000.00"))
    await broker.account()  # a drawdown read must not lower the baseline
    mark = await broker.equity_mark()
    assert mark.high_water == 100000.0, mark
    broker._client = lambda: _FakeClient(account=_account_with("110000.00"))
    await broker.account()  # a new high ratchets the baseline up
    mark = await broker.equity_mark()
    assert mark.high_water == 110000.0 and mark.marked_at, mark

    # a deposit or withdrawal re-baselines to the live equity, note recorded
    mark = await broker.reset_equity_mark("paper account reset")
    assert mark.high_water == 110000.0 and "paper account reset" in mark.note, mark
    broker._client = lambda: _FakeClient(account=_account_with("90000.00"))
    mark = await broker.reset_equity_mark()
    assert mark.high_water == 90000.0 and "deposit or withdrawal" in mark.note, mark
    stored = await broker.equity_mark()
    assert stored.high_water == 90000.0, stored

    # the GET endpoint is a pure local read: no broker call behind it
    def _no_broker():
        raise AssertionError("the mark endpoint must not reach the broker")
    broker._client = _no_broker
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        body = (await client.get("/api/broker/equity-mark")).json()
        assert body["high_water"] == 90000.0 and "deposit or withdrawal" in body["note"], body
    print("equity high-water mark OK (seeds, holds on dips, ratchets, resets with a note)")

    _restore()

    # ---- the endpoint refreshes a stale stored state and serves the sweep --
    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET
    fake = _FakeClient(account=_raw_account())
    stray2 = _remote_order("bta-stray-2", status="new")
    stray2.symbol = "AMC"
    stray2.side = SimpleNamespace(value="sell")
    stray2.notional = "10.00"
    stray2.submitted_at = ""
    fake.open_orders = [stray2]
    fake.by_client_id[partial_id] = _remote_order(
        partial_id, filled_qty="10", filled_avg_price="99.5"
    )
    _use(fake)
    # The stored state is an hour old (the sweep-failure test wrote it): the
    # endpoint must run one fresh sweep instead of serving the stale result.
    stale = {
        "checked_at": time.time() - 3600, "in_sync": True,
        "reconciled": 0, "drift": [], "error": None,
    }
    await asyncio.to_thread(broker._write_sync_state, stale)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        body = (await client.get("/api/broker/sync")).json()
        assert body["in_sync"] is False and body["drift"][0]["symbol"] == "AMC", body
        assert body["checked_at"] is not None and body["error"] is None
    print("sync endpoint OK (stale stored state triggers one fresh sweep)")

    _restore()
    settings.alpaca_api_key_id = ""
    settings.alpaca_api_secret_key = ""


if __name__ == "__main__":
    asyncio.run(checks())
    print("\nAll broker sync checks passed.")
