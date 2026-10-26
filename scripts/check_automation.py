"""Offline checks for automated paper-trading sessions (autopilot).

Run: PYTHONPATH=. uv run python scripts/check_automation.py
No network and no real runs: the broker client seam, the market discovery,
and the run store are replaced with fakes, so every scenario is
deterministic and key-less.
"""

import asyncio
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

# Isolated DB and cleared env before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "automation_test.db"
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

import httpx  # noqa: E402

from app import automation, broker, run_history  # noqa: E402
import app.discovery as discovery  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models import RunStatus, StockAnalysis  # noqa: E402
from app.runs import store  # noqa: E402
from app.tools import market_data  # noqa: E402

FAKE_KEY = "PKCHECKKEY"
FAKE_SECRET = "secret-check-value-must-never-leak"
RECENT = datetime.now(timezone.utc).isoformat()

REAL_CLIENT = broker._client
REAL_DISCOVER = discovery.discover_stocks
REAL_CACHED = discovery._cached_candidates
REAL_STORE_CREATE = store.create
REAL_BEAR = automation._bear_market
REAL_CLOSES = market_data.get_closes_between
REAL_HTTPX = automation.httpx


async def _no_bear():
    return None  # unknown -> the brake fails open


def _use(fake):
    broker._client = lambda: fake


def _restore():
    broker._client = REAL_CLIENT
    discovery.discover_stocks = REAL_DISCOVER
    discovery._cached_candidates = REAL_CACHED
    store.create = REAL_STORE_CREATE
    automation._bear_market = REAL_BEAR
    market_data.get_closes_between = REAL_CLOSES
    automation.httpx = REAL_HTTPX


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

    def get_orders(self, filter=None):  # noqa: A002 - mirrors the SDK's name
        return []  # wash-trade pre-check: nothing working unless a test says so

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
        if not automation._session_lock.locked():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("session did not finish in time")


async def checks() -> None:
    await run_history.init()
    await broker.init()
    await automation.init()
    automation._bear_market = _no_bear  # offline: no SPY fetch in these tests

    # ---- disabled by default; the toggle round-trips and persists ----------
    assert await automation.enabled() is False
    await automation.set_enabled(True)
    assert await automation.enabled() is True
    await automation.set_enabled(False)
    assert await automation.enabled() is False
    print("toggle persists OK")

    # ---- discovery rotation: recent picks are held back, never starved ------
    def _trend(step: float) -> list[float]:
        return [100 * (1 + step) ** i for i in range(200)]

    pool = {"AAA": 0.004, "BBB": 0.003, "CCC": 0.002, "DDD": 0.001, "EEE": 0.0005}
    snapshot = (
        ({ticker: _trend(step) for ticker, step in pool.items()},
         {ticker: 2_000_000_000 for ticker in pool}),
        False,
    )
    discovery._cached_candidates = lambda: snapshot
    try:
        plain = await discovery.discover_stocks("short_term", 3)
        assert plain["tickers"] == ["AAA", "BBB", "CCC"], plain["tickers"]
        rotated = await discovery.discover_stocks("short_term", 3, exclude={"AAA", "BBB"})
        assert rotated["tickers"] == ["CCC", "DDD", "EEE"], rotated["tickers"]
        # Everything excluded: the strongest names backfill instead of failing.
        starved = await discovery.discover_stocks("short_term", 3, exclude=set(pool))
        assert starved["tickers"] == ["AAA", "BBB", "CCC"], starved["tickers"]
        assert rotated["excluded_tickers"] == ["AAA", "BBB"]
    finally:
        discovery._cached_candidates = REAL_CACHED
    print("discovery rotation OK (fresh first, strongest backfill, no-exclude unchanged)")

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
    async def _dead_discovery(outlook, limit, exclude=()):
        raise ValueError("not enough current market data")

    discovery.discover_stocks = _dead_discovery
    fake = _FakeClient(account=_raw_account(), positions=[])
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "skipped" and "discovery failed" in session.error
    print("discovery failure with no holdings -> skipped OK")

    # ---- happy path: one scan -> one run -> guarded orders ------------------
    async def _fake_discovery(outlook, limit, exclude=()):
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
    # Held positions come first, so the session cap can never drop one.
    assert session.tickers == ["MSFT", "GOOG", "NVDA"], session.tickers
    assert session.decisions == {
        "NVDA": "BUY", "AMD": "BUY", "MSFT": "SELL", "GOOG": "BUY", "TSLA": "HOLD",
    }
    placed = [(o.ticker, o.side, o.status) for o in session.orders]
    assert placed == [("NVDA", "buy", "accepted"), ("MSFT", "sell", "accepted")], placed
    assert fake.submit_calls[0].symbol == "NVDA" and fake.submit_calls[0].notional == 5000
    # A sell is haircut to 99.5% of the holding so it can never short.
    assert fake.submit_calls[1].symbol == "MSFT"
    assert abs(fake.submit_calls[1].notional - 1990.0) < 0.01
    assert not automation._session_lock.locked()
    # The session row carries the full picture for the UI.
    snapshot = await automation.status_snapshot()
    latest = snapshot.sessions[0]
    assert latest.status == "completed" and latest.run_id == "autorun01"
    assert latest.decisions["TSLA"] == "HOLD" and len(latest.orders) == 2
    print("happy path OK (dedup, sizing, sell haircut, held guard, confidence bar)")

    # ---- candidate rotation: the last session's picks are held back ---------
    captured_excludes: list[set[str]] = []

    async def _capturing_discovery(outlook, limit, exclude=()):
        captured_excludes.append(set(exclude))
        return {"tickers": ["AAPL"]}

    discovery.discover_stocks = _capturing_discovery
    fake = _FakeClient(
        account=_raw_account(),
        positions=[_raw_position("MSFT", "2000.00"), _raw_position("GOOG", "1500.00")],
    )
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    assert {"NVDA", "MSFT", "GOOG"} <= captured_excludes[0], captured_excludes

    settings.automation_candidate_cooldown_days = 0
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    assert captured_excludes[1] == set(), captured_excludes[1]
    settings.automation_candidate_cooldown_days = 3
    print("candidate rotation OK (recent picks excluded; cooldown 0 disables)")

    # ---- held positions are always checked, even at the session cap ---------
    held8 = [f"HELD{i}" for i in range(8)]
    cap_results = {t: _analysis(t, "HOLD", 0.5) for t in held8 + ["CAND1", "CAND2", "CAND3"]}

    async def _cap_discovery(outlook, limit, exclude=()):
        return {"tickers": ["CAND1", "CAND2", "CAND3"]}

    async def _cap_create(tickers, client_id="", outlook="", depth=""):
        return _FakeRun("caprun01", {t: cap_results[t] for t in tickers})

    discovery.discover_stocks = _cap_discovery
    store.create = _cap_create
    fake = _FakeClient(
        account=_raw_account(),
        positions=[_raw_position(t, "1000.00") for t in held8],
    )
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    assert set(held8) <= set(session.tickers), session.tickers
    assert len(session.tickers) == automation.MAX_SESSION_TICKERS
    # 8 held + 3 candidates = 11 > cap: the rotating candidate is the one cut.
    assert sum(t.startswith("CAND") for t in session.tickers) == 2, session.tickers
    print("held positions survive the session cap OK")

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

    # ---- P2.4: bear-market brake (BUYs brake, research and exits continue) --
    async def _bear_true():
        return True

    # A fresh run id: autorun01 already carries active orders from the happy
    # path, and the duplicate-open-order guard would reject the exit here.
    await run_history.save(
        RunStatus(
            run_id="autorun03", tickers=list(results), status="completed",
            started_at=0.0, duration_s=1.0, results=results,
        ),
        owner_id="automation_check",
    )

    async def _brake_run(tickers, client_id="", outlook="", depth=""):
        return _FakeRun("autorun03", results)

    automation._bear_market = _bear_true
    store.create = _brake_run
    fake = _FakeClient(
        account=_raw_account(),
        positions=[_raw_position("MSFT", "2000.00"), _raw_position("GOOG", "1500.00")],
    )
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    braked = [o for o in session.orders if o.ticker == "NVDA"]
    assert len(braked) == 1 and braked[0].status == "braked", session.orders
    assert "200-day" in (braked[0].error or "")
    assert [c.symbol for c in fake.submit_calls] == ["MSFT"], fake.submit_calls
    automation._bear_market = _no_bear
    store.create = _run_for
    print("bear-market brake OK (BUY braked with reason, SELL exit still sent)")

    # _bear_market itself: below/above the average, short history, dead feed
    def _series(values):
        base = date(2026, 1, 1)
        return {(base + timedelta(days=i)).isoformat(): v for i, v in enumerate(values)}

    async def _closes(values):
        async def _fake(ticker, start, end):
            assert ticker == "SPY"
            return _series(values)

        return _fake

    async def _short_closes(ticker, start, end):
        return _series([100.0] * 150)

    async def _dead_closes(ticker, start, end):
        raise RuntimeError("no data")

    automation._bear_market = REAL_BEAR  # unit-test the real detector below
    market_data.get_closes_between = await _closes([100.0] * 250 + [80.0])
    assert await automation._bear_market() is True
    market_data.get_closes_between = await _closes([100.0] * 250 + [130.0])
    assert await automation._bear_market() is False
    market_data.get_closes_between = _short_closes
    assert await automation._bear_market() is None
    market_data.get_closes_between = _dead_closes
    assert await automation._bear_market() is None
    market_data.get_closes_between = REAL_CLOSES
    automation._bear_market = _no_bear
    print("bear-market detection OK (below, above, short history and dead feed fail open)")

    # ---- P2.4: portfolio budget cap + cash floor -----------------------------
    await run_history.save(
        RunStatus(
            run_id="budgetrun", tickers=["XYZ"], status="completed",
            started_at=0.0, duration_s=1.0, results={},
        ),
        owner_id="automation",
    )
    with broker._connect() as connection:
        connection.execute(
            "INSERT INTO broker_orders (client_order_id, run_id, ticker, side, "
            "notional, status) VALUES ('bta-budget-1', 'budgetrun', 'XYZ', "
            "'buy', 80000.0, 'filled')"
        )
        connection.execute(
            "INSERT INTO broker_orders (client_order_id, run_id, ticker, side, "
            "notional, status) VALUES ('bta-budget-2', 'budgetrun', 'OLD', "
            "'sell', 10000.0, 'filled')"
        )
    # Manual runs (owner 'automation_check', like every earlier fake here) and
    # unfilled exits must not move the number: 80k buy - 10k filled sell = 70k.
    assert automation._automation_deployed() == 70000.0
    settings.automation_max_deployed_pct = 0.5  # 50k cap on 100k equity
    fake = _FakeClient(account=_raw_account(), positions=[])
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    budget = [o for o in session.orders if o.ticker == "NVDA"]
    assert len(budget) == 1 and budget[0].status == "skipped", session.orders
    assert "budget" in (budget[0].error or "")
    assert fake.submit_calls == [], fake.submit_calls
    settings.automation_max_deployed_pct = 1.0

    settings.automation_cash_floor_pct = 0.6  # 60k floor, only 50k cash
    session = await automation.run_session("schedule")
    floor = [o for o in session.orders if o.ticker == "NVDA"]
    assert len(floor) == 1 and floor[0].status == "skipped", session.orders
    assert "cash floor" in (floor[0].error or "")
    settings.automation_cash_floor_pct = 0.0
    print("portfolio budget OK (own buys minus filled sells; cap and floor skip)")

    # ---- items 11-13: weight sizing, turnover cap, overlay cooldown ----------
    # Isolate this section: earlier scenarios' orders (including the 70k budget
    # fixture above) would otherwise consume the deployment budget and make
    # every expected notional below depend on their accumulated total. The
    # per-order cap lifts too: turnover math needs orders bigger than $10k to
    # be meaningful, and the two caps are independent knobs.
    with broker._connect() as connection:
        connection.execute("DELETE FROM broker_orders")
    settings.alpaca_max_order_usd = 50000.0

    # ---- item 11: a recorded target weight sizes the order -------------------
    weighted = {"NVDA": _analysis("NVDA", "BUY", 0.85, size=5000.0)}
    weighted["NVDA"].target_weight_pct = 3.0  # 3% of $100k equity = $3,000
    await run_history.save(
        RunStatus(
            run_id="weightrun", tickers=["NVDA"], status="completed",
            started_at=0.0, duration_s=1.0, results=weighted,
        ),
        owner_id="automation_check",
    )

    async def _weight_run(tickers, client_id="", outlook="", depth=""):
        return _FakeRun("weightrun", weighted)

    async def _single_discovery(outlook, limit, exclude=()):
        return {"tickers": ["NVDA"]}

    store.create = _weight_run
    discovery.discover_stocks = _single_discovery
    fake = _FakeClient(account=_raw_account(), positions=[])
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    assert [c.symbol for c in fake.submit_calls] == ["NVDA"], fake.submit_calls
    assert fake.submit_calls[0].notional == 3000.0, fake.submit_calls[0].notional
    print("item 11 OK: target weight sizes the order ($3,000 = 3% of $100k)")

    # ---- item 12: turnover cap scales the buy side proportionally ------------
    trio = {t: _analysis(t, "BUY", 0.9, size=30000.0) for t in ("AMZN", "NFLX", "ORCL")}
    await run_history.save(
        RunStatus(
            run_id="turnrun", tickers=list(trio), status="completed",
            started_at=0.0, duration_s=1.0, results=trio,
        ),
        owner_id="automation_check",
    )

    async def _turn_run(tickers, client_id="", outlook="", depth=""):
        return _FakeRun("turnrun", trio)

    async def _trio_discovery(outlook, limit, exclude=()):
        return {"tickers": ["AMZN", "NFLX", "ORCL"]}

    store.create = _turn_run
    discovery.discover_stocks = _trio_discovery
    fake = _FakeClient(account=_raw_account(), positions=[])
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    # $90k proposed vs the 50% ($50k) cap: every buy scales by 5/9.
    submitted = {c.symbol: c.notional for c in fake.submit_calls}
    assert set(submitted) == {"AMZN", "NFLX", "ORCL"}, submitted
    assert all(abs(n - 16666.67) < 0.01 for n in submitted.values()), submitted
    print("item 12 OK: turnover cap scaled $90k of buys to $50k total")

    # Exits are never scaled: a sell consumes budget first and goes out whole.
    mixed = {
        "MSFT": _analysis("MSFT", "SELL", 0.7),
        "AMZN": _analysis("AMZN", "BUY", 0.9, size=30000.0),
    }
    await run_history.save(
        RunStatus(
            run_id="turnrun2", tickers=list(mixed), status="completed",
            started_at=0.0, duration_s=1.0, results=mixed,
        ),
        owner_id="automation_check",
    )

    async def _turn_run2(tickers, client_id="", outlook="", depth=""):
        return _FakeRun("turnrun2", mixed)

    async def _pair_discovery(outlook, limit, exclude=()):
        return {"tickers": ["AMZN"]}

    store.create = _turn_run2
    discovery.discover_stocks = _pair_discovery
    fake = _FakeClient(
        account=_raw_account(), positions=[_raw_position("MSFT", "40000.00")]
    )
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    submitted = {c.symbol: c.notional for c in fake.submit_calls}
    # The $40k exit (haircut to $39,800 at submit) leaves $10k of the budget.
    assert abs(submitted["MSFT"] - 39800.0) < 0.01, submitted
    assert abs(submitted["AMZN"] - 10000.0) < 0.01, submitted
    print("item 12 OK: exits never scale; the buy takes what is left ($10,000)")

    # ---- item 13: overlay cooldown blocks re-entry ---------------------------
    from app import overlay as overlay_mod

    await overlay_mod.init()
    settings.overlay_enabled = True

    async def _no_closes(ticker, start, end):
        return {}

    market_data.get_closes_between = _no_closes
    future = (datetime.now(timezone.utc).date() + timedelta(days=5)).isoformat()
    overlay_mod._set_cooldown("NVDA", future)
    store.create = _weight_run
    discovery.discover_stocks = _single_discovery
    fake = _FakeClient(account=_raw_account(), positions=[])
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    nvda = [o for o in session.orders if o.ticker == "NVDA"]
    assert len(nvda) == 1 and nvda[0].status == "skipped", session.orders
    assert "cooldown" in (nvda[0].error or ""), nvda
    assert fake.submit_calls == [], fake.submit_calls

    # An expired cooldown releases the ticker (fresh run: the first scenario's
    # order for weightrun/NVDA is still active and would trip the dedupe).
    overlay_mod._set_cooldown("NVDA", "2020-01-01")
    await run_history.save(
        RunStatus(
            run_id="weightrun2", tickers=["NVDA"], status="completed",
            started_at=0.0, duration_s=1.0, results=weighted,
        ),
        owner_id="automation_check",
    )

    async def _weight_run2(tickers, client_id="", outlook="", depth=""):
        return _FakeRun("weightrun2", weighted)

    store.create = _weight_run2
    fake = _FakeClient(account=_raw_account(), positions=[])
    _use(fake)
    session = await automation.run_session("schedule")
    nvda = [o for o in session.orders if o.ticker == "NVDA"]
    assert nvda and nvda[0].status == "accepted", session.orders
    settings.overlay_enabled = False
    market_data.get_closes_between = REAL_CLOSES
    settings.alpaca_max_order_usd = 10000.0
    print("item 13 OK: cooldown blocks re-entry, expiry releases it")

    # ---- P2.4: webhook - one plain-text POST per finished session ------------
    webhook_posts = []

    class _HookClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, content=None, headers=None):
            webhook_posts.append((url, content, headers))

    automation.httpx = SimpleNamespace(AsyncClient=_HookClient)
    settings.webhook_url = "https://hooks.example/abc"

    fake = _FakeClient(
        account=_raw_account(),
        clock=SimpleNamespace(is_open=False, next_open="tomorrow"),
    )
    _use(fake)
    skipped = await automation.run_session("schedule")
    assert skipped.status == "skipped" and webhook_posts == [], webhook_posts

    fake = _FakeClient(
        account=_raw_account(),
        positions=[_raw_position("MSFT", "2000.00"), _raw_position("GOOG", "1500.00")],
    )
    _use(fake)
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error
    assert len(webhook_posts) == 1, webhook_posts  # one POST, not one per order
    url, body, headers = webhook_posts[0]
    assert url == "https://hooks.example/abc"
    assert headers["Content-Type"] == "text/plain"
    assert isinstance(body, str) and "completed" in body and "NVDA=BUY" in body, body
    webhook_posts.clear()

    store.create = _crashing_run
    session = await automation.run_session("schedule")
    assert session.status == "failed"
    assert len(webhook_posts) == 1 and "failed" in webhook_posts[0][1]
    webhook_posts.clear()

    class _RaisingHookClient(_HookClient):
        async def post(self, url, content=None, headers=None):
            raise RuntimeError("hook down")

    automation.httpx = SimpleNamespace(AsyncClient=_RaisingHookClient)
    store.create = _run_for
    session = await automation.run_session("schedule")
    assert session.status == "completed", session.error  # a dead hook never fails a session
    settings.webhook_url = ""
    print("webhook OK (one POST per finished session, plain text, failures tolerated)")

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
        assert body["allow_sells"] is True and body["depth"] == "fast"

        toggled = await client.post("/api/automation", json={"enabled": True})
        assert toggled.status_code == 200 and toggled.json()["enabled"] is True
        assert await automation.enabled() is True
        off = await client.post("/api/automation", json={"enabled": False})
        assert off.json()["enabled"] is False

        await automation._session_lock.acquire()  # simulate a session in flight
        conflict = await client.post("/api/automation/run")
        assert conflict.status_code == 409, conflict.text
        automation._session_lock.release()

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
