"""One-off sanity checks for the quick-win changes. Run: uv run python scripts/check_quick_wins.py"""

import asyncio
import os
import random
import sqlite3
import tempfile
from pathlib import Path

# Isolated DB before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "portfolio_test.db"
os.environ["DB_PATH"] = str(_TMP)

from app.tools.indicators import compute_indicators, macd  # noqa: E402

# ---- indicators -----------------------------------------------------------
random.seed(7)
closes = [100.0]
for _ in range(129):
    closes.append(closes[-1] * (1 + random.uniform(-0.02, 0.02)))
highs = [c * 1.01 for c in closes]
lows = [c * 0.99 for c in closes]
volumes = [1_000_000 + int(random.uniform(-200_000, 400_000)) for _ in closes]
volumes[-1] = 3_000_000  # volume spike on the last day

ind = compute_indicators(closes, highs, lows, volumes)
for key in ("macd", "macd_signal", "macd_histogram", "bollinger_percent_b",
            "bollinger_width_pct", "atr_14", "atr_pct_of_price",
            "avg_volume_20d", "relative_volume"):
    assert key in ind, f"missing {key}"
assert ind["atr_14"] > 0 and ind["atr_pct_of_price"] > 0
assert ind["relative_volume"] > 2, "volume spike not detected"

up = [100 * (1.005**i) for i in range(60)]
m = macd(up)
assert m and m["macd"] > 0 and m["macd_histogram"] > 0, "uptrend MACD should be positive"

short = compute_indicators([1, 2, 3, 4, 5], [2, 3, 4, 5, 6], [0, 1, 2, 3, 4], [10] * 5)
assert "macd" not in short and "atr_14" not in short and "bollinger_percent_b" not in short
print("indicators OK:", {k: ind[k] for k in ("macd", "macd_histogram",
      "bollinger_percent_b", "bollinger_width_pct", "atr_14", "atr_pct_of_price",
      "relative_volume")})

# ---- rebuttal mocks (deterministic, offline) --------------------------------
from app.agents import bear, bull  # noqa: E402

_round = {"score": 0.8, "summary": "own argument"}
_opp = {"score": 0.7, "summary": "opponent argument"}
_bull_rebuttal = bull.mock("NVDA", {"own_round_1": _round, "opponent_round_1": _opp}, rebuttal=True)
_bear_rebuttal = bear.mock("NVDA", {"own_round_1": _opp, "opponent_round_1": _round}, rebuttal=True)
assert 0.0 <= _bull_rebuttal["score"] <= 1.0 and "Rebuttal" in _bull_rebuttal["summary"]
assert 0.0 <= _bear_rebuttal["score"] <= 1.0 and "Rebuttal" in _bear_rebuttal["summary"]
print("rebuttal mocks OK:", _bull_rebuttal["score"], _bear_rebuttal["score"])

# ---- portfolio: cash guard, close, history ---------------------------------
from app import portfolio as pf  # noqa: E402


async def fake_price(_ticker):
    return 110.0


pf.get_current_price = fake_price  # keep the smoke test offline


async def portfolio_checks():
    await pf.init()
    p1 = await pf.add_position("NVDA", 10, entry_price=100.0)
    await pf.add_position("AMD", 5, entry_price=200.0)

    try:
        await pf.add_position("TSLA", 10000, entry_price=500.0)
        raise AssertionError("cash guard did not trigger")
    except ValueError as exc:
        print("cash guard OK:", exc)

    closed = await pf.close_position(p1.id, exit_price=150.0)
    assert closed.pnl == 500.0 and closed.pnl_pct == 50.0 and closed.closed_at

    for _ in range(2):
        try:
            await pf.close_position(p1.id, exit_price=150.0)
            raise AssertionError("double close allowed")
        except LookupError:
            pass
    print("double close OK")

    try:
        await pf.close_position(999, exit_price=1.0)
        raise AssertionError("missing id allowed")
    except LookupError:
        print("missing id OK")

    summary = await pf.get_portfolio()
    # 100000 - 2000 (both buys) + 1500 (NVDA proceeds) = 99500
    assert summary.cash == 99500, summary.cash
    assert summary.realized_pnl == 500.0
    assert [p.ticker for p in summary.positions] == ["AMD"]
    assert [h.ticker for h in summary.history] == ["NVDA"]
    # equity = cash + 5 * 110 (AMD at fake price) = 100050
    # total P&L 50 = realized +500 on NVDA plus unrealized -450 on AMD
    assert summary.total_equity == 100050, summary.total_equity
    assert summary.total_pnl == 50
    print("portfolio OK: cash=%s realized=%s equity=%s"
          % (summary.cash, summary.realized_pnl, summary.total_equity))


asyncio.run(portfolio_checks())

# ---- schema migration from the pre-close DB layout -------------------------
old_db = Path(tempfile.mkdtemp()) / "old_portfolio.db"
con = sqlite3.connect(old_db)
con.execute(
    "CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, "
    "quantity REAL NOT NULL, entry_price REAL NOT NULL, "
    "added_at TEXT NOT NULL DEFAULT (datetime('now')))"
)
con.execute("INSERT INTO positions (ticker, quantity, entry_price) VALUES ('MSFT', 2, 300.0)")
con.commit()
con.close()

pf.settings.db_path = old_db


async def migration_checks():
    await pf.init()  # ALTER TABLE adds exit_price / closed_at
    summary = await pf.get_portfolio()
    assert [p.ticker for p in summary.positions] == ["MSFT"]
    closed = await pf.close_position(summary.positions[0].id, exit_price=400.0)
    assert closed.pnl == 200.0
    print("migration OK: old-schema DB upgraded and closeable")


asyncio.run(migration_checks())

# ---- workflow imports (catches syntax / import errors everywhere) ----------
import app.main  # noqa: E402, F401

print("app.main import OK")
print("ALL CHECKS PASSED")
