"""Offline checks for concurrency and run-lifecycle hardening (ROADMAP P2.8).

Run: PYTHONPATH=. uv run python scripts/check_lifecycle.py
No network and no real runs: the broker client seam, the run store's analysis
path, and discovery are replaced with fakes, so every scenario is
deterministic and key-less. Covers the 2026-10-03 audit races: the autopilot
session lock, the serialized submit guards, the BUY position re-check, the
run deadline, hung in-flight analysis cleanup, the SSE replay snapshot, the
finished-run memory grace, and the corrupt automation row guard.
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

# Isolated DB and cleared env before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "lifecycle_test.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["BAKO_API_KEY"] = ""
os.environ["ALPACA_API_KEY_ID"] = ""
os.environ["ALPACA_API_KEY"] = ""
os.environ["ALPACA_SECRET_KEY"] = ""
os.environ["ALPACA_TRADING_ENABLED"] = ""
os.environ["AUTOMATION_ENABLED"] = ""

from app import automation, broker, run_history, runs  # noqa: E402
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
REAL_ANALYZE_ONE = store._analyze_one
REAL_ANALYZE_TICKER = runs.analyze_ticker
REAL_STORE_CREATE = store.create


def _use(fake):
    broker._client = lambda: fake


def _restore():
    broker._client = REAL_CLIENT
    discovery.discover_stocks = REAL_DISCOVER
    store._analyze_one = REAL_ANALYZE_ONE
    runs.analyze_ticker = REAL_ANALYZE_TICKER
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


def _raw_position(symbol: str, market_value: str = "1000.00") -> SimpleNamespace:
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
    """Quacks like runs.Run with preset results."""

    def __init__(self, run_id: str, results: dict[str, StockAnalysis]):
        self.run_id = run_id
        self.results = results
        self.execution_task = asyncio.create_task(self._execute())

    async def _execute(self):
        return None

    def to_status(self) -> RunStatus:
        return RunStatus(
            run_id=self.run_id,
            tickers=list(self.results),
            status="completed",
            started_at=0.0,
            duration_s=1.0,
            results=self.results,
        )


def _analysis(ticker: str, decision: str, confidence: float) -> StockAnalysis:
    return StockAnalysis(
        ticker=ticker,
        price=100.0,
        decision=decision,  # type: ignore[arg-type]
        manager_decision=decision,  # mirrors the real workflow's successful run
        confidence=confidence,
        as_of=RECENT,
    )


async def _wait_finished(task: asyncio.Task, timeout: float = 10.0) -> None:
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        raise AssertionError("task did not finish in time")


async def checks() -> None:
    await run_history.init()
    await broker.init()
    await automation.init()

    # ---- the run deadline fails a hung run ----------------------------------
    settings.run_timeout_minutes = 0.001  # 0.06 s; a real config clamps to >= 5

    async def _hanging_analysis(run, ticker, portfolio=None, held_closes=None):
        await asyncio.sleep(30)

    store._analyze_one = _hanging_analysis
    run = await store.create(["SLOW"], client_id="lifecycle_check")
    await _wait_finished(run.execution_task)
    assert run.status == "failed", run.status
    assert "timed out" in (run.error or ""), run.error
    row = await run_history.get(run.run_id)
    assert row is not None and row.status == "failed" and "timed out" in (row.error or "")
    settings.run_timeout_minutes = 30
    store._analyze_one = REAL_ANALYZE_ONE
    print("run deadline OK (hung run fails, persists as failed)")

    # ---- hung in-flight analysis is cancelled with its last waiter ----------
    release = asyncio.Event()

    async def _blocked_analyze(ticker, emit, **_kwargs):
        await emit("ticker_started", {"ticker": ticker})
        await release.wait()
        return _analysis(ticker, "HOLD", 0.5)

    runs.analyze_ticker = _blocked_analyze
    first = await store.create(["STCK"], client_id="lifecycle_check", incognito=True)
    await asyncio.sleep(0.1)  # let the analysis task start and register
    inflight_key = next(iter(store.analysis_inflight))
    assert not store.analysis_inflight[inflight_key].done()
    first_task = store.analysis_inflight[inflight_key]
    await store.cancel(first.run_id)
    for _ in range(100):
        if inflight_key not in store.analysis_inflight and (
            first_task.cancelled() or first_task.done()
        ):
            break
        await asyncio.sleep(0.02)
    assert inflight_key not in store.analysis_inflight, "zombie analysis left in the map"
    assert first_task.cancelled() or first_task.done(), "shared analysis not stopped"
    release.set()
    runs.analyze_ticker = REAL_ANALYZE_TICKER
    print("in-flight cleanup OK (last waiter cancels the shared analysis)")

    # ---- finished runs leave memory after the reconnect grace ---------------
    async def _instant_analysis(run, ticker, portfolio=None, held_closes=None):
        return _analysis(ticker, "HOLD", 0.5)

    store._analyze_one = _instant_analysis
    quick = await store.create(["FAST"], client_id="lifecycle_check")
    await _wait_finished(quick.execution_task)
    assert quick.status == "completed"
    assert store.get(quick.run_id) is not None, "grace period must keep the run"
    status = await store.get_status(quick.run_id)
    assert status is not None and "FAST" in status.results
    quick.completed_at = time.time() - runs.FINISHED_RUN_GRACE_SECONDS - 10
    after = await store.create(["FAST2"], client_id="lifecycle_check")
    await _wait_finished(after.execution_task)
    assert store.get(quick.run_id) is None, "expired run must leave memory"
    status = await store.get_status(quick.run_id)
    assert status is not None and "FAST" in status.results, "DB fallback lost the run"
    print("finished-run eviction OK (grace in memory, then served from SQLite)")

    # ---- SSE replay: no event is delivered twice ----------------------------
    unblock = asyncio.Event()

    async def _streamable_analysis(run, ticker, portfolio=None, held_closes=None):
        await run.emit("ticker_started", {"ticker": ticker})
        await unblock.wait()
        return _analysis(ticker, "HOLD", 0.5)

    store._analyze_one = _streamable_analysis
    sse_run = await store.create(["SSE1"], client_id="lifecycle_check", incognito=True)
    messages: list[dict] = []

    async def _receive():
        await asyncio.sleep(3600)  # the request body is never read; no disconnect
        return {"type": "http.disconnect"}

    async def _send(message):
        if message["type"] == "http.response.body":
            messages.append(message)

    def _events_so_far() -> list[dict]:
        raw = b"".join(m.get("body", b"") for m in messages)
        out = []
        for chunk in raw.split(b"\n\n"):
            chunk = chunk.strip()
            if chunk.startswith(b"data: "):
                out.append(json.loads(chunk[len(b"data: "):]))
        return out

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/runs/{sse_run.run_id}/events",
        "raw_path": f"/api/runs/{sse_run.run_id}/events".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }
    # Drive the real endpoint directly: httpx's ASGITransport buffers a
    # streaming response until it ends, which would hide the mid-stream race.
    app_task = asyncio.create_task(app(scope, _receive, _send))
    for _ in range(200):
        if any(e["type"] == "ticker_started" for e in _events_so_far()):
            break
        await asyncio.sleep(0.02)
    assert any(e["type"] == "ticker_started" for e in _events_so_far()), "replay never arrived"
    # Emit while the replay loop sits between yields: the snapshot must keep
    # the queue from delivering this a second time.
    await sse_run.emit("marker", {"n": 1})
    for _ in range(200):
        if any(e["type"] == "marker" for e in _events_so_far()):
            break
        await asyncio.sleep(0.02)
    unblock.set()
    for _ in range(200):
        if any(e["type"] == "analysis_completed" for e in _events_so_far()):
            break
        await asyncio.sleep(0.02)
    await asyncio.wait({app_task}, timeout=5)
    seen = _events_so_far()
    markers = [e for e in seen if e["type"] == "marker"]
    assert len(markers) == 1, f"marker delivered {len(markers)} times"
    assert seen[-1]["type"] == "analysis_completed"
    store._analyze_one = REAL_ANALYZE_ONE
    print("SSE replay OK (event emitted mid-replay delivered exactly once)")

    # ---- concurrent submit_order: exactly one passes the guards -------------
    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET
    settings.alpaca_trading_enabled = True
    results = {"NVDA": _analysis("NVDA", "BUY", 0.8)}
    await run_history.save(
        RunStatus(
            run_id="liferun01", tickers=["NVDA"], status="completed",
            started_at=0.0, duration_s=1.0, results=results,
        ),
        owner_id="lifecycle_check",
    )
    fake = _FakeClient(account=_raw_account())

    def _slow_submit(order_data):
        fake.submit_calls.append(order_data)
        time.sleep(0.05)  # widen the window the guards must stay serialized over
        row = broker._get_order_row(order_data.client_order_id)
        assert row is not None
        return SimpleNamespace(
            id=uuid4(), status="accepted", filled_qty=None, filled_avg_price=None,
        )

    fake.submit_order = _slow_submit
    _use(fake)
    attempts = [
        broker.submit_order("liferun01", "NVDA", "buy", 10.0) for _ in range(5)
    ]
    outcomes = await asyncio.gather(*attempts, return_exceptions=True)
    ok = [o for o in outcomes if not isinstance(o, Exception)]
    conflicts = [
        o for o in outcomes
        if isinstance(o, broker.BrokerRuleError) and o.status_code == 409
    ]
    assert len(ok) == 1, [str(o) for o in outcomes]
    assert len(conflicts) == 4, [str(o) for o in outcomes]
    assert len(fake.submit_calls) == 1, "more than one POST left the guards"
    print("submit serialization OK (5 concurrent, 1 accepted, 4 x 409, 1 POST)")

    settings.alpaca_trading_enabled = False
    settings.alpaca_api_key_id = ""
    settings.alpaca_api_secret_key = ""

    # ---- autopilot session lock: two triggers never run at once -------------
    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET
    settings.alpaca_trading_enabled = True
    settings.llm_api_key = FAKE_KEY
    fake = _FakeClient(account=_raw_account())
    _use(fake)

    async def _fake_discovery(outlook, limit, exclude=()):
        return {"tickers": ["NVDA"]}

    discovery.discover_stocks = _fake_discovery

    async def _slow_create(tickers, client_id="", outlook="", depth=""):
        await asyncio.sleep(0.3)
        return _FakeRun("lockrun01", {"NVDA": _analysis("NVDA", "HOLD", 0.5)})

    store.create = _slow_create
    first_session = asyncio.create_task(automation.run_session("schedule"))
    await asyncio.sleep(0.05)
    assert automation._session_lock.locked(), "session never took the lock"
    snapshot = await automation.status_snapshot()
    assert snapshot.session_active is True
    second_session = await automation.run_session("manual")
    assert second_session.status == "skipped"
    assert "already running" in (second_session.error or "")
    await _wait_finished(first_session)
    assert not automation._session_lock.locked()
    assert first_session.result().status == "completed"
    print("session lock OK (manual trigger skipped while schedule holds it)")

    # ---- BUY re-checks positions at order time, not session start -----------
    fake = _FakeClient(account=_raw_account())  # no positions yet
    _use(fake)
    buy_results = {"NVDA": _analysis("NVDA", "BUY", 0.9)}

    async def _position_appears(tickers, client_id="", outlook="", depth=""):
        # A fill lands between the session-start snapshot and ordering.
        fake.positions.append(_raw_position("NVDA"))
        return _FakeRun("buyrechk1", buy_results)

    store.create = _position_appears
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    assert session.orders == [], "a BUY fired for a ticker that was held by then"
    print("BUY position re-check OK (position filled mid-session, no order)")

    # ---- a corrupt session row renders as an empty session ------------------
    corrupt_id = automation._insert_session("schedule", 0.0)

    def _corrupt():
        import sqlite3

        connection = sqlite3.connect(settings.db_path)
        try:
            connection.execute(
                "UPDATE automation_sessions SET tickers_json = '{not json' WHERE id = ?",
                (corrupt_id,),
            )
            connection.commit()
        finally:
            connection.close()

    await asyncio.to_thread(_corrupt)
    rows = await asyncio.to_thread(automation._list_sessions, 50)
    rendered = next(row for row in rows if row.id == corrupt_id)
    assert rendered.tickers == [] and rendered.decisions == {}
    print("corrupt session row OK (listing survives, renders empty)")

    _restore()
    settings.alpaca_api_key_id = ""
    settings.alpaca_api_secret_key = ""
    settings.alpaca_trading_enabled = False
    settings.llm_api_key = ""


if __name__ == "__main__":
    asyncio.run(checks())
    print("\nAll lifecycle checks passed.")
