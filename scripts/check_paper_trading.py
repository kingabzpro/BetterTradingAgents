"""Offline checks for Alpaca paper trading (ROADMAP P2.1 M1+M2).

Run: PYTHONPATH=. uv run python scripts/check_paper_trading.py
No network and no broker calls: the Alpaca client seam is replaced with a
fake, so every scenario is deterministic and key-less.
"""

import asyncio
import json
import os
import sqlite3
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
os.environ["ALPACA_API_KEY"] = ""
os.environ["ALPACA_SECRET_KEY"] = ""
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
        self.account = account
        self.positions = positions
        self.error = error
        self.submit_calls = 0
        self.cancel_calls = 0
        self.submit_error = None
        self.on_submit = None
        self.remote_order = None
        self.history = None

    def get_account(self):
        if self.error:
            raise self.error
        return self.account

    def get_all_positions(self):
        if self.error:
            raise self.error
        return self.positions

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
        if self.error:
            raise self.error
        return self.remote_order

    def get_portfolio_history(self, history_filter=None):
        if self.error:
            raise self.error
        return self.history

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
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        read = await client.get("/api/broker/status")
        assert read.status_code == 200 and read.json()["configured"] is False

        missing = await client.get("/api/broker/account")
        assert missing.status_code == 503, missing.text
        assert "Settings page" in missing.json()["detail"]

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
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
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
    await _rejects("paperrun01", "AAPL", "buy", 20000, 400, "must not exceed")
    await _rejects("paperrun01", "AAPL", "sell", 100, 400, "not a sell")
    await _rejects("paperrun01", "AAPL", "buy", 0.5, 400, "at least 1 USD")
    print("guard rails OK (HOLD, no position, stale, caps, side mismatch)")


    # ---- kill switch and confirm are endpoint-level --------------------------
    settings.alpaca_trading_enabled = False
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        blocked = await client.post(
            "/api/broker/orders",
            json={"run_id": "paperrun01", "ticker": "AAPL", "side": "buy", "notional": 100, "confirm": True},
        )
        assert blocked.status_code == 403, blocked.text
        assert "disabled" in blocked.json()["detail"]
    settings.alpaca_trading_enabled = True
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
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
    fake.on_submit = None

    # ---- live-discovered edge cases (2026-09-29) -----------------------------
    # Alpaca accepts at most 2 decimals on notional; buys are rounded.
    captured = {}
    fake.on_submit = lambda order_data: captured.update(notional=order_data.notional)
    rounded = await broker.submit_order("paperrun01", "aapl", "buy", 5000.567)
    assert captured["notional"] == 5000.57, captured["notional"]
    assert rounded.notional == 5000.57
    print("buy notional rounded to 2dp OK")

    # A dust position (worth < $1) must still be closable: the sell floor is
    # the position value after the anti-overshoot haircut, not $1.
    dust = SimpleNamespace(
        symbol="AAPL", qty="0.0013", avg_entry_price="764.72",
        current_price="769.0", market_value="0.5",
        unrealized_pl="0.003", unrealized_plpc="0.003",
    )
    fake.positions = [dust]
    dust_order = await broker.submit_order("paperrun05", "AAPL", "sell", 100)
    assert captured["notional"] == 0.5, captured["notional"]  # min(100, 0.4975) rounded
    assert dust_order.status == "accepted"
    fake.positions = []
    fake.on_submit = None
    print("dust-position sell below $1 OK")

    # ================= M3: orders list with reconcile-on-read =================
    # Rows are newest first; a non-terminal row reconciles from the remote
    # order, and an unreachable Alpaca degrades to the local row.
    stale_local = SimpleNamespace(
        id=uuid4(), status=OrderStatus.FILLED, filled_qty="7", filled_avg_price="191.00",
    )
    fake.submit_error = None
    await broker.submit_order("paperrun03", "AMD", "buy", 900.0)  # local row: accepted
    fake.remote_order = None
    fake.error = _api_error(503, 50310000, "temporarily down")
    orders = await broker.list_orders(limit=10)
    fake.error = None
    assert orders[0].ticker == "AMD" and orders[0].status == "accepted", orders[0]
    assert any(o.status == "rejected" for o in orders), "the 422 row must persist"
    print("list_orders degrade-to-local OK")

    fake.remote_order = stale_local
    orders = await broker.list_orders(limit=10)
    amd = next(o for o in orders if o.ticker == "AMD" and o.status == "filled")
    assert amd.filled_qty == 7.0 and amd.filled_avg_price == 191.0
    print("reconcile-on-read OK")

    # ---- endpoint: list + cancel through the API -----------------------------
    _use(fake)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            listed = await client.get("/api/broker/orders?limit=5")
            assert listed.status_code == 200 and len(listed.json()) <= 5

            placed = await client.post(
                "/api/broker/orders",
                json={"run_id": "paperrun03", "ticker": "AMD", "side": "buy", "notional": 700, "confirm": True},
            )
            assert placed.status_code == 200, placed.text
            fresh = placed.json()
            assert fresh["status"] == "accepted"

            fake.remote_order = SimpleNamespace(
                id=uuid4(), status=OrderStatus.CANCELED, filled_qty=None, filled_avg_price=None,
            )
            canceled = await client.delete(f"/api/broker/orders/{fresh['client_order_id']}")
            assert canceled.status_code == 200, canceled.text
            assert canceled.json()["status"] == "canceled"

            missing = await client.delete("/api/broker/orders/bta-nope")
            assert missing.status_code == 404
        print("orders endpoint OK")
    finally:
        _restore()

    # ================= M5: equity curve and performance loop ==================
    from datetime import date, timedelta

    from app.tools import market_data

    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    fake.history = SimpleNamespace(
        timestamp=[1_759_000_000, 1_759_086_400],
        equity=[100000.0, 101000.0],
        profit_loss=[0.0, 1000.0],
        profit_loss_pct=[0.0, 0.01],
    )
    _use(fake)
    try:
        curve = await broker.equity("1M")
        assert curve["period"] == "1M" and len(curve["dates"]) == 2
        assert curve["equity"] == [100000.0, 101000.0]
        fake.history = SimpleNamespace(
            timestamp=[1_759_000_000, 1_759_086_400],
            equity=[100000.0, None],  # Alpaca pads the last slot intraday
            profit_loss=[0.0, 0.0],
            profit_loss_pct=[0.0, 0.0],
        )
        curve = await broker.equity("1M")
        assert curve["equity"] == [100000.0], "None marks must be dropped"
        # Zero-padded history before account creation is noise, not a baseline.
        fake.history = SimpleNamespace(
            timestamp=[1_759_000_000, 1_759_086_400, 1_759_172_800, 1_759_259_200],
            equity=[0.0, 0.0, 100000.0, 101000.0],
            profit_loss=[0.0, 0.0, 0.0, 0.0],
            profit_loss_pct=[0.0, 0.0, 0.0, 0.0],
        )
        curve = await broker.equity("1M")
        assert curve["equity"] == [100000.0, 101000.0], "zero marks must be dropped"
        print("equity mapping OK")

        # ---- per-order return math and alpha vs SPY on fake closes ------------
        async def fake_spy(ticker, start, end):
            assert ticker == "SPY"
            return {today: 100.0, tomorrow: 105.0}

        async def fake_live_price(ticker):
            return 110.0

        market_data.get_closes_between = fake_spy
        market_data.get_current_price = fake_live_price
        # P2.9: a SELL's alpha is the BUY formula inverted - a good exit is
        # the ticker lagging SPY afterwards.
        with sqlite3.connect(os.environ["DB_PATH"]) as connection:
            connection.execute(
                "INSERT INTO broker_orders (client_order_id, run_id, ticker, side,"
                " notional, status, filled_qty, filled_avg_price)"
                " VALUES ('bta-sell-perf', 'paperrun06', 'MSFT', 'sell', 400,"
                " 'filled', 2, 200.0)"
            )
        perf = await broker.order_performance()
        filled = [row for row in perf if row["filled_avg_price"] == 191.0]
        assert filled, perf
        row = filled[0]
        assert row["current_price"] == 110.0
        assert row["return_pct"] == round((110.0 / 191.0 - 1) * 100, 2)
        assert row["spy_return_pct"] == 5.0
        assert row["alpha_pct"] == round(row["return_pct"] - 5.0, 2)
        sell = next(r for r in perf if r["client_order_id"] == "bta-sell-perf")
        assert sell["alpha_pct"] == round(5.0 - sell["return_pct"], 2), sell
        assert all(row["run_id"] for row in perf), "every perf row links to its run"
        print("filled-order return + alpha math OK (sell-side sign inverted)")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            curve_response = await client.get("/api/broker/equity?period=1M")
            assert curve_response.status_code == 200
            assert len(curve_response.json()["equity"]) == 2
            perf_response = await client.get("/api/broker/performance")
            assert perf_response.status_code == 200 and perf_response.json()
            bad_period = await client.get("/api/broker/equity?period=2W")
            assert bad_period.status_code == 422
        print("equity + performance endpoints OK")
    finally:
        _restore()

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
