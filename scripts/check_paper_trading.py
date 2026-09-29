"""Offline checks for Alpaca paper trading (ROADMAP P2.1 M1+M2).

Run: PYTHONPATH=. uv run python scripts/check_paper_trading.py
No network and no broker calls: the Alpaca client seam is replaced with a
fake, so every scenario is deterministic and key-less.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4

# Isolated DB and cleared Alpaca env before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "paper_trading_test.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""
os.environ["ALPACA_API_KEY_ID"] = ""
os.environ["ALPACA_API_SECRET_KEY"] = ""
os.environ["ALPACA_TRADING_ENABLED"] = ""

import httpx  # noqa: E402
import requests  # noqa: E402
from alpaca.common.exceptions import APIError  # noqa: E402
from alpaca.trading.enums import AccountStatus, OrderStatus  # noqa: E402

from app import broker, run_history  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models import RunStatus, StockAnalysis  # noqa: E402

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


class _FakeClient:
    """The smallest object that quacks like TradingClient for one scenario."""

    def __init__(self, account=None, positions=None, error=None):
        self._account = account
        self._positions = positions
        self._error = error
        self.submit_calls = 0
        self.cancel_calls = 0
        self.submit_error = None
        self.on_submit = None
        self.remote_order = None

    def get_account(self):
        if self._error:
            raise self._error
        return self._account

    def get_all_positions(self):
        if self._error:
            raise self._error
        return self._positions

    def submit_order(self, order_data):
        self.submit_calls += 1
        if self.on_submit:
            self.on_submit(order_data)
        if self.submit_error:
            raise self.submit_error
        return SimpleNamespace(
            id=uuid4(),
            status=OrderStatus.ACCEPTED,
            filled_qty=None,
            filled_avg_price=None,
        )

    def get_order_by_client_id(self, client_order_id):
        if self._error:
            raise self._error
        return self.remote_order

    def cancel_order_by_id(self, order_id):
        self.cancel_calls += 1


def _raw_account(**overrides):
    raw = SimpleNamespace(
        account_number="PA-TEST-123",
        status=AccountStatus.ACTIVE,
        equity="101234.56",
        cash="51234.56",
        buying_power="202469.12",
        last_equity="100000.00",
        trading_blocked=False,
    )
    for key, value in overrides.items():
        setattr(raw, key, value)
    return raw


async def checks() -> None:
    # ---- unconfigured: 503 with a setup hint, secrets absent -----------------
    assert not settings.alpaca_configured
    status = await broker.status()
    assert status.configured is False and status.enabled is False
    assert "alpaca.markets" in status.paper_url

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        read = await client.get("/api/broker/status")
        assert read.status_code == 200 and read.json()["configured"] is False

        missing = await client.get("/api/broker/account")
        assert missing.status_code == 503, missing.text
        assert "ALPACA_API_KEY_ID" in missing.json()["detail"]

        positions = await client.get("/api/broker/positions")
        assert positions.status_code == 503
    print("unconfigured 503 + hint OK")

    # ---- configured: the client seam is paper-only and carries the keys ------
    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET
    settings.alpaca_trading_enabled = False
    try:
        real = broker._client()
        assert type(real).__name__ == "TradingClient"
        assert real._api_key == FAKE_KEY and real._secret_key == FAKE_SECRET
        base = real._base_url
        base = base.value if hasattr(base, "value") else base
        assert "paper-api.alpaca.markets" in str(base)
    finally:
        settings.alpaca_api_key_id = ""
        settings.alpaca_api_secret_key = ""
    print("paper-only client construction OK")

    # ---- mapping through the seam: account -----------------------------------
    fake = _FakeClient(account=_raw_account())
    _use(fake)
    try:
        account = await broker.account()
        assert account.account_number == "PA-TEST-123"
        assert account.status == "ACTIVE"
        assert account.equity == 101234.56 and account.cash == 51234.56
        assert account.buying_power == 202469.12 and account.last_equity == 100000.0
        assert account.trading_blocked is False
        print("account mapping OK")

        # Empty money fields stay None, never 0.
        empty = await _account_from(_raw_account(equity="", cash=None, trading_blocked=True))
        assert empty.equity is None and empty.cash is None
        assert empty.trading_blocked is True
        print("blank money fields -> None OK")
    finally:
        _restore()

    # ---- mapping through the seam: positions ---------------------------------
    fake = _FakeClient(
        positions=[
            SimpleNamespace(
                symbol="AAPL",
                qty="12",
                avg_entry_price="190.25",
                current_price="195.50",
                market_value="2346.00",
                unrealized_pl="63.00",
                unrealized_plpc="0.0276",
            ),
            SimpleNamespace(
                symbol="SPY",
                qty="1",
                avg_entry_price="500",
                current_price="",
                market_value="",
                unrealized_pl="",
                unrealized_plpc="",
            ),
        ]
    )
    _use(fake)
    try:
        rows = await broker.positions()
        assert rows[0].symbol == "AAPL" and rows[0].quantity == 12
        assert rows[0].avg_entry_price == 190.25 and rows[0].current_price == 195.5
        assert rows[0].market_value == 2346.0 and rows[0].unrealized_plpc == 0.0276
        assert rows[1].current_price is None and rows[1].unrealized_pl is None
        print("positions mapping OK")
    finally:
        _restore()

    # ---- error mapping --------------------------------------------------------
    cases = [
        (_api_error(401, 40110000, "bad key"), "check the ALPACA API keys"),
        (_api_error(429, 42910000, "rate limit"), "rate limit"),
        (_api_error(422, 42210000, "market closed"), "market closed"),
    ]
    for exc, expected in cases:
        text = broker._map_broker_error(exc)
        assert expected in text, (expected, text)
        assert FAKE_SECRET not in text
    print("error mapping OK")

    # ---- credentials never reach any payload ---------------------------------
    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET
    fake = _FakeClient(account=_raw_account(), positions=[])
    _use(fake)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/api/broker/status", "/api/broker/account", "/api/broker/positions"):
                body = (await client.get(path)).text
                assert FAKE_SECRET not in body and FAKE_KEY not in body, path
        print("secrets absent from every payload OK")
    finally:
        _restore()

    # ================= M2: submission contract ================================
    await run_history.init()
    await broker.init()
    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET
    settings.alpaca_trading_enabled = True
    settings.alpaca_max_order_usd = 10000.0
    settings.alpaca_max_orders_per_day = 20
    settings.alpaca_max_decision_age_hours = 72.0
    recent = datetime.now(timezone.utc).isoformat()

    def _run(run_id: str, ticker: str, decision: str, as_of: str = recent) -> RunStatus:
        return RunStatus(
            run_id=run_id,
            tickers=[ticker],
            status="completed",
            started_at=1_790_000_000,
            duration_s=10.0,
            results={
                ticker: StockAnalysis(
                    ticker=ticker, price=190.0, decision=decision,  # type: ignore[arg-type]
                    confidence=0.7, as_of=as_of,
                )
            },
        )

    for saved in (
        _run("paperrun01", "AAPL", "BUY"),
        _run("paperrun02", "NVDA", "BUY"),
        _run("paperrun03", "AMD", "BUY"),
        _run("paperrun04", "MSFT", "HOLD"),
        _run("paperrun05", "AAPL", "SELL"),
        _run("paperrun06", "TSLA", "BUY", as_of="2026-01-01T00:00:00+00:00"),
    ):
        await run_history.save(saved, completed_at=1_790_000_010.0, owner_id="paper_check")

    active_account = _raw_account()
    fake = _FakeClient(account=active_account, positions=[])
    _use(fake)

    # ---- guard rails reject before any HTTP call -----------------------------
    async def _rejects(run_id, ticker, side, notional, status_code, phrase):
        submits_before = fake.submit_calls
        try:
            await broker.submit_order(run_id, ticker, side, notional)
            raise AssertionError(f"{run_id} {side} must be rejected")
        except broker.BrokerRuleError as exc:
            assert exc.status_code == status_code, (exc.status_code, exc)
            assert phrase in str(exc), str(exc)
            assert fake.submit_calls == submits_before, "a guard fired after the POST"

    await _rejects("paperrun04", "MSFT", "buy", 100, 400, "HOLD is never traded")
    await _rejects("paperrun05", "AAPL", "sell", 100, 400, "no paper position")
    await _rejects("paperrun06", "TSLA", "buy", 100, 409, "research too old")
    await _rejects("paperrun01", "AAPL", "buy", 20000, 400, "notional must be between")
    await _rejects("paperrun01", "AAPL", "sell", 100, 400, "not a sell")
    print("guard rails OK (HOLD, no position, stale, notional cap, side mismatch)")

    # ---- kill switch and confirm are endpoint-level --------------------------
    settings.alpaca_trading_enabled = False
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        blocked = await client.post(
            "/api/broker/orders",
            json={"run_id": "paperrun01", "ticker": "AAPL", "side": "buy", "notional": 100, "confirm": True},
        )
        assert blocked.status_code == 403, blocked.text
        assert "disabled" in blocked.json()["detail"]
    settings.alpaca_trading_enabled = True
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unconfirmed = await client.post(
            "/api/broker/orders",
            json={"run_id": "paperrun01", "ticker": "AAPL", "side": "buy", "notional": 100, "confirm": False},
        )
        assert unconfirmed.status_code == 400 and "confirm" in unconfirmed.json()["detail"]
        bad_ticker = await client.post(
            "/api/broker/orders",
            json={"run_id": "paperrun01", "ticker": "!!", "side": "buy", "notional": 100, "confirm": True},
        )
        assert bad_ticker.status_code == 400
    print("kill switch + confirm + ticker validation OK")

    # ---- happy submit: pending_submit row exists BEFORE the POST -------------
    def _assert_pending(order_data):
        row = broker._get_order_row(order_data.client_order_id)
        assert row is not None and row["status"] == "pending_submit"

    fake.on_submit = _assert_pending
    placed = await broker.submit_order("paperrun01", "aapl", "buy", 5000.0)
    assert fake.submit_calls == 1
    assert placed.status == "accepted" and placed.alpaca_order_id
    assert placed.decision == "BUY" and placed.run_id == "paperrun01"
    row = broker._get_order_row(placed.client_order_id)
    assert row["status"] == "accepted" and row["notional"] == 5000.0
    assert row["decision_as_of"] == recent
    print("happy submit + insert-before-POST OK:", placed.client_order_id[:12])

    # ---- duplicate while non-terminal -> 409 ---------------------------------
    try:
        await broker.submit_order("paperrun01", "AAPL", "buy", 5000.0)
        raise AssertionError("duplicate must be rejected")
    except broker.BrokerRuleError as exc:
        assert exc.status_code == 409
    assert fake.submit_calls == 1
    print("duplicate 409 OK")

    # ---- timeout: state unknown, reconcile-by-client-id, no second POST ------
    fake.submit_error = requests.exceptions.ConnectionError("connection lost")
    lost = await broker.submit_order("paperrun02", "NVDA", "buy", 2500.0)
    assert lost.status == "unknown" and "unknown" in (lost.error or "")
    assert fake.submit_calls == 2  # AAPL + NVDA, never a retry POST
    fake.submit_error = None
    fake.remote_order = SimpleNamespace(
        id=uuid4(), status=OrderStatus.FILLED, filled_qty="12.5", filled_avg_price="195.50",
    )
    recovered = await broker.refresh_order(lost.client_order_id)
    assert fake.submit_calls == 2, "recovery must be a lookup, never a second POST"
    assert recovered.status == "filled"
    assert recovered.filled_qty == 12.5 and recovered.filled_avg_price == 195.5
    print("timeout -> unknown -> reconcile OK")

    # ---- cancel ---------------------------------------------------------------
    fake.remote_order = SimpleNamespace(
        id=uuid4(), status=OrderStatus.CANCELED, filled_qty=None, filled_avg_price=None,
    )
    canceled = await broker.cancel_order(placed.client_order_id)
    assert canceled.status == "canceled" and fake.cancel_calls == 1
    try:
        await broker.cancel_order(placed.client_order_id)
        raise AssertionError("cancel of a terminal order must 422")
    except broker.BrokerRuleError as exc:
        assert exc.status_code == 422
    assert fake.cancel_calls == 1
    print("cancel + terminal 422 OK")

    # ---- daily cap ------------------------------------------------------------
    settings.alpaca_max_orders_per_day = 1
    await _rejects("paperrun03", "AMD", "buy", 100, 429, "daily paper order cap")
    settings.alpaca_max_orders_per_day = 20
    print("daily cap OK")

    # ---- rejected by Alpaca: the POST happens, the row settles, no position --
    import sqlite3

    fake.submit_error = _api_error(422, 42210000, "market closed")
    submits_before = fake.submit_calls
    try:
        await broker.submit_order("paperrun03", "AMD", "buy", 100)
        raise AssertionError("a 422 from Alpaca must not pass as an order")
    except broker.BrokerRuleError as exc:
        assert exc.status_code == 502 and "market closed" in str(exc)
    assert fake.submit_calls == submits_before + 1, "the POST happened exactly once"
    with sqlite3.connect(settings.db_path) as connection:
        status = connection.execute(
            "SELECT status FROM broker_orders WHERE ticker = 'AMD' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert status == "rejected", status
    fake.submit_error = None
    print("Alpaca 422 mapping + rejected row OK")

    _restore()
    settings.alpaca_api_key_id = ""
    settings.alpaca_api_secret_key = ""
    settings.alpaca_trading_enabled = False


async def _account_from(raw) -> broker.BrokerAccount:
    _use(_FakeClient(account=raw))
    try:
        return await broker.account()
    finally:
        _restore()


asyncio.run(checks())
print("PAPER TRADING CHECKS PASSED")
