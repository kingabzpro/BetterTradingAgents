"""Offline checks for automated paper-trading sessions (autopilot).

Run: PYTHONPATH=. uv run python scripts/check_automation.py
No network and no real runs: the broker client seam, the market discovery,
and the run store are replaced with fakes, so every scenario is
deterministic and key-less.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

# Isolated DB and cleared env before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "automation_test.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""
os.environ["ALPACA_API_KEY_ID"] = ""
os.environ["ALPACA_API_KEY"] = ""
os.environ["ALPACA_SECRET_KEY"] = ""
os.environ["ALPACA_TRADING_ENABLED"] = ""
os.environ["AUTOMATION_ENABLED"] = ""

import httpx  # noqa: E402

from app import automation, broker, run_history  # noqa: E402
import app.discovery as discovery  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models import RunStatus, StockAnalysis  # noqa: E402
from app.runs import store  # noqa: E402

FAKE_KEY = "PKCHECKKEY"
FAKE_SECRET = "secret-check-value-must-never-leak"
RECENT = datetime.now(timezone.utc).isoformat()

REAL_CLIENT = broker._client
REAL_DISCOVER = discovery.discover_stocks
REAL_STORE_CREATE = store.create


def _use(fake):
    broker._client = lambda: fake


def _restore():
    broker._client = REAL_CLIENT
    discovery.discover_stocks = REAL_DISCOVER
    store.create = REAL_STORE_CREATE


class _FakeClient:
    """The smallest object that quacks like TradingClient for one scenario."""

    def __init__(self, account=None, positions=None, clock=None):
        self.account = account
        self.positions = positions or []
        self.clock = clock or SimpleNamespace(is_open=True, next_open="")
        self.submit_calls: list = []

    def get_clock(self):
        return self.clock

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        assert order_data.client_order_id
        row = broker._get_order_row(order_data.client_order_id)
        assert row is not None and row["status"] == "pending_submit", (
            "the local row must exist before the POST"
        )
        return SimpleNamespace(
            id=uuid4(),
            status="accepted",
            filled_qty=None,
            filled_avg_price=None,
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


def _raw_position(symbol: str, market_value: str) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        qty="10",
        avg_entry_price="100",
        current_price="100",
        market_value=market_value,
        unrealized_pl="0",
        unrealized_plpc="0",
    )


class _FakeRun:
    """Quacks like runs.Run with preset results; the analysis it cites is
    persisted in run_history separately so broker guards validate."""

    def __init__(self, run_id: str, results: dict[str, StockAnalysis], crash: Exception | None = None):
        self.run_id = run_id
        self.results = results
        self.crash = crash
        self.execution_task = asyncio.create_task(self._execute())

    async def _execute(self):
        if self.crash:
            raise self.crash

    def to_status(self) -> RunStatus:
        return RunStatus(
            run_id=self.run_id,
            tickers=list(self.results),
            status="completed",
            started_at=0.0,
            duration_s=1.0,
            results=self.results,
        )


def _analysis(ticker: str, decision: str, confidence: float, size: float | None = None) -> StockAnalysis:
    return StockAnalysis(
        ticker=ticker,
        price=100.0,
        decision=decision,  # type: ignore[arg-type]
        confidence=confidence,
        suggested_size_usd=size,
        as_of=RECENT,
    )


async def _wait_session_done(timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if not automation._session_active:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("session did not finish in time")


async def checks() -> None:
    await run_history.init()
    await broker.init()
    await automation.init()

    # ---- disabled by default; the toggle round-trips and persists ----------
    assert await automation.enabled() is False
    await automation.set_enabled(True)
    assert await automation.enabled() is True
    await automation.set_enabled(False)
    assert await automation.enabled() is False
    print("toggle persists OK")

    # ---- skip ladder: every gate records its reason, nothing trades --------
    session = await automation.run_session("schedule")
    assert session.status == "skipped" and "not configured" in session.error

    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET
    settings.alpaca_trading_enabled = False
    session = await automation.run_session("schedule")
    assert session.status == "skipped" and "kill switch" in session.error

    settings.alpaca_trading_enabled = True
    session = await automation.run_session("schedule")
    assert session.status == "skipped" and "mock" in session.error

    settings.llm_api_key = FAKE_KEY
    fake = _FakeClient(account=_raw_account(), clock=SimpleNamespace(is_open=False, next_open="tomorrow 9:30"))
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "skipped" and "market closed" in session.error
    print("skip ladder OK (unconfigured, kill switch, mock, market closed)")

    # ---- discovery dead + nothing held: skipped, not crashed ----------------
    async def _dead_discovery(outlook, limit):
        raise ValueError("not enough current market data")

    discovery.discover_stocks = _dead_discovery
    fake = _FakeClient(account=_raw_account(), positions=[])
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "skipped" and "discovery failed" in session.error
    print("discovery failure with no holdings -> skipped OK")

    # ---- happy path: one scan -> one run -> guarded orders ------------------
    async def _fake_discovery(outlook, limit):
        assert limit == settings.automation_candidates
        return {"tickers": ["NVDA", "MSFT", "GOOG"]}

    discovery.discover_stocks = _fake_discovery
    results = {
        "NVDA": _analysis("NVDA", "BUY", 0.80, size=5000.0),   # trades
        "AMD": _analysis("AMD", "BUY", 0.50, size=4000.0),     # below the bar
        "MSFT": _analysis("MSFT", "SELL", 0.70),               # exits the holding
        "GOOG": _analysis("GOOG", "BUY", 0.90, size=4000.0),   # already held: never add
        "TSLA": _analysis("TSLA", "HOLD", 0.60),               # nothing to do
    }
    await run_history.save(
        RunStatus(
            run_id="autorun01", tickers=list(results), status="completed",
            started_at=0.0, duration_s=1.0, results=results,
        ),
        owner_id="automation_check",
    )
    async def _fake_create(tickers, client_id="", outlook="", depth=""):
        return _FakeRun("autorun01", results)

    store.create = _fake_create
    fake = _FakeClient(
        account=_raw_account(),
        positions=[_raw_position("MSFT", "2000.00"), _raw_position("GOOG", "1500.00")],
    )
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    assert session.run_id == "autorun01"
    assert session.tickers == ["NVDA", "MSFT", "GOOG"], session.tickers
    assert session.decisions == {
        "NVDA": "BUY", "AMD": "BUY", "MSFT": "SELL", "GOOG": "BUY", "TSLA": "HOLD",
    }
    placed = [(o.ticker, o.side, o.status) for o in session.orders]
    assert placed == [("NVDA", "buy", "accepted"), ("MSFT", "sell", "accepted")], placed
    assert fake.submit_calls[0].symbol == "NVDA" and fake.submit_calls[0].notional == 5000
    # A sell is haircut to 99.5% of the holding so it can never short.
    assert fake.submit_calls[1].symbol == "MSFT"
    assert abs(fake.submit_calls[1].notional - 1990.0) < 0.01
    assert automation._session_active is False
    # The session row carries the full picture for the UI.
    snapshot = await automation.status_snapshot()
    latest = snapshot.sessions[0]
    assert latest.status == "completed" and latest.run_id == "autorun01"
    assert latest.decisions["TSLA"] == "HOLD" and len(latest.orders) == 2
    print("happy path OK (dedup, sizing, sell haircut, held guard, confidence bar)")

    # ---- a crashed analysis run records a failed session --------------------
    async def _crashing_run(tickers, client_id="", outlook="", depth=""):
        return _FakeRun("autorun02", {}, crash=RuntimeError("boom"))

    store.create = _crashing_run
    session = await automation.run_session("schedule")
    assert session.status == "failed" and "boom" in session.error
    print("failed run recorded OK")

    # ---- order rejection does not kill the session --------------------------
    async def _run_for(tickers, client_id="", outlook="", depth=""):
        return _FakeRun("autorun01", results)

    store.create = _run_for
    settings.alpaca_max_order_usd = 10.0  # every BUY now exceeds the cap
    session = await automation.run_session("schedule")
    assert session.status == "completed"
    assert all(o.status == "rejected" and "exceed" in (o.error or "") for o in session.orders)
    settings.alpaca_max_order_usd = 10000.0
    print("rejected orders recorded, session survives OK")

    # ---- stale 'running' rows settle honestly on init -----------------------
    stale_id = automation._insert_session("schedule", 0.0)
    await automation.init()
    rows = await asyncio.to_thread(automation._list_sessions, 200)
    settled = next(row for row in rows if row.id == stale_id)
    assert settled.status == "failed" and "restarted" in (settled.error or "")
    print("stale running session settled OK")

    # ---- the sessions table is pruned ---------------------------------------
    for _ in range(automation.SESSIONS_KEPT + 10):
        automation._insert_session("schedule", 0.0)
    rows = await asyncio.to_thread(automation._list_sessions, 10_000)
    assert len(rows) <= automation.SESSIONS_KEPT
    print(f"session pruning OK (kept {len(rows)})")

    # ---- API: status, toggle, run-now guards --------------------------------
    await automation.set_enabled(False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        read = await client.get("/api/automation")
        assert read.status_code == 200
        body = read.json()
        assert body["enabled"] is False and body["configured"] is True
        assert body["interval_minutes"] == 240 and body["min_confidence"] == 0.65
        assert body["allow_sells"] is True and body["depth"] == "medium"

        toggled = await client.post("/api/automation", json={"enabled": True})
        assert toggled.status_code == 200 and toggled.json()["enabled"] is True
        assert await automation.enabled() is True
        off = await client.post("/api/automation", json={"enabled": False})
        assert off.json()["enabled"] is False

        automation._session_active = True  # simulate a session in flight
        conflict = await client.post("/api/automation/run")
        assert conflict.status_code == 409, conflict.text
        automation._session_active = False

        started = await client.post("/api/automation/run")
        assert started.status_code == 200 and started.json() == {"started": True}
        await _wait_session_done()
        snapshot = await automation.status_snapshot()
        assert snapshot.sessions[0].trigger == "manual"
    print("API status + toggle + run-now OK")

    _restore()
    settings.alpaca_api_key_id = ""
    settings.alpaca_api_secret_key = ""
    settings.alpaca_trading_enabled = False
    settings.llm_api_key = ""


if __name__ == "__main__":
    asyncio.run(checks())
    print("\nAll automation checks passed.")
