"""Offline checks for the risk overlay (release batch 3, item 13).

Pure trigger math, plus the full run_check path with the broker stubbed:
signals, exits, cooldown, rejected orders, and stale-state cleanup. No
network and no LLM.

Run: uv run python -m scripts.check_overlay
"""

import asyncio
import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "overlay_test.db")

from app import broker, overlay  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import BrokerPosition  # noqa: E402
from app.tools import market_data  # noqa: E402


def position(symbol: str, entry: float, current: float, value: float | None = None):
    return BrokerPosition(
        symbol=symbol,
        quantity=100,
        avg_entry_price=entry,
        current_price=current,
        market_value=value if value is not None else current * 100,
    )


async def checks() -> None:
    await overlay.init()

    # ---- pure trigger math ---------------------------------------------------
    assert overlay.evaluate(100, 100, 87, 0.12, 0.15) == "absolute_stop"  # -13% from entry
    assert overlay.evaluate(100, 100, 88, 0.12, 0.15) == "absolute_stop"  # exactly at the stop
    assert overlay.evaluate(100, 120, 90, 0.12, 0.15) == "trailing_stop"  # -25% from peak
    assert overlay.evaluate(100, 100, 95, 0.12, 0.15) is None  # inside both levels
    assert overlay.evaluate(100, 80, 92, 0.12, 0.15) is None  # peak below entry never trails
    assert overlay.evaluate(0, 100, 50, 0.12, 0.15) is None  # no entry price, no call
    print("overlay trigger math OK")

    # ---- run_check: signal-only mode, then execution -------------------------
    calls = {"exits": []}
    crash_on = {"ticker": None}

    async def fake_positions():
        return [
            position("CRASH", 100, 80),   # absolute stop
            position("TRAIL", 100, 90, value=9000.0),  # peak will be seeded low
            position("SAFE", 100, 99),    # nothing
        ]

    async def fake_exit(ticker, notional, reason):
        if ticker == crash_on["ticker"]:
            raise RuntimeError("broker said no")
        calls["exits"].append((ticker, notional, reason))

        class Order:
            status = "accepted"
            notional = 0.0
            client_order_id = "bta-test"

        order = Order()
        order.notional = notional
        return order

    async def fake_closes(ticker, start, end):
        # TRAIL peaked at 130 while held; CRASH never ran up.
        return {"2026-09-01": 130.0} if ticker == "TRAIL" else {}

    broker.positions = fake_positions
    broker.submit_exit_order = fake_exit
    market_data.get_closes_between = fake_closes
    settings.overlay_stop_loss_pct = 0.12
    settings.overlay_trailing_stop_pct = 0.15
    settings.overlay_cooldown_days = 5

    # signal-only: nothing executes; both stops report (TRAIL's peak is
    # seeded from the history at 130, so 90 is -30% from it)
    signals = await overlay.run_check(execute=False)
    signalled = {s.ticker: s.trigger for s in signals}
    assert signalled == {
        "CRASH": "absolute_stop",
        "TRAIL": "trailing_stop",
    }, signalled
    assert calls["exits"] == [], calls
    print("run_check signalled OK:", signalled)

    # execute: a fresh state table, both positions exit
    settings.overlay_cooldown_days = 5
    with broker._connect() as connection:
        connection.execute("DELETE FROM overlay_state")
    exits = await overlay.run_check(execute=True)
    executed = {e.ticker: e.status for e in exits}
    assert executed == {"CRASH": "accepted", "TRAIL": "accepted"}, executed
    exited = {t for t, _, _ in calls["exits"]}
    assert exited == {"CRASH", "TRAIL"}, calls
    reasons = {t: r for t, _, r in calls["exits"]}
    assert "absolute_stop" in reasons["CRASH"] and "trailing_stop" in reasons["TRAIL"]
    # cooldown set for both, and the stopped tickers show up
    assert overlay.cooldown_until("CRASH") is not None
    assert overlay.cooldown_until("TRAIL") is not None
    assert overlay.cooldown_until("SAFE") is None
    print("run_check execute OK:", executed)

    # ---- rejected exits still record the stop and the cooldown ----------------
    crash_on["ticker"] = "CRASH"
    with broker._connect() as connection:
        connection.execute("DELETE FROM overlay_state")
    calls["exits"].clear()
    exits = await overlay.run_check(execute=True)
    by_ticker = {e.ticker: e for e in exits}
    assert by_ticker["CRASH"].status == "rejected" and "broker said no" in (
        by_ticker["CRASH"].error or ""
    ), by_ticker["CRASH"]
    assert overlay.cooldown_until("CRASH") is not None
    crash_on["ticker"] = None
    print("rejected exit OK: state and cooldown still recorded")

    # ---- expired cooldown reads as None --------------------------------------
    overlay._set_cooldown("SAFE", "2020-01-01")
    assert overlay.cooldown_until("SAFE") is None
    print("expired cooldown OK")

    # ---- stale cleanup: price state forgotten once the position is closed ----
    async def only_safe():
        return [position("SAFE", 100, 99)]

    broker.positions = only_safe
    await overlay.run_check(execute=False)
    row = overlay._read_state("CRASH")
    assert row is not None and row["entry_price"] == 0, row  # forgotten, not deleted
    assert overlay.cooldown_until("CRASH") is not None  # cooldown survives
    print("stale cleanup OK")


asyncio.run(checks())
print("OVERLAY CHECKS PASSED")
