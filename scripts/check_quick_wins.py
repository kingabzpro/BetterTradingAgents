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

from app.tools.indicators import compute_indicators, historical_forecast, macd  # noqa: E402
from app.tools.market_data import _normalize_timegpt_forecast  # noqa: E402
from app import discovery  # noqa: E402
from app.discovery import rank_candidates  # noqa: E402

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
            "avg_volume_20d", "relative_volume", "forecast_price_5d",
            "forecast_change_5d_pct", "forecast_trend_r2"):
    assert key in ind, f"missing {key}"
assert ind["atr_14"] > 0 and ind["atr_pct_of_price"] > 0
assert ind["relative_volume"] > 2, "volume spike not detected"

up = [100 * (1.005**i) for i in range(60)]
m = macd(up)
assert m and m["macd"] > 0 and m["macd_histogram"] > 0, "uptrend MACD should be positive"
forecast = historical_forecast(up)
assert forecast and forecast["forecast_price_5d"] > up[-1]
assert forecast["forecast_change_5d_pct"] > 0 and forecast["forecast_trend_r2"] > 0.99
assert historical_forecast([1, 2, 3]) is None
timegpt = _normalize_timegpt_forecast({"mean": [101, 102, 103, 104, 105]}, 100, 5)
assert timegpt and timegpt["forecast_price_5d"] == 105
assert timegpt["forecast_change_5d_pct"] == 5 and timegpt["forecast_method"] == "timegpt-1"

short = compute_indicators([1, 2, 3, 4, 5], [2, 3, 4, 5, 6], [0, 1, 2, 3, 4], [10] * 5)
assert "macd" not in short and "atr_14" not in short and "bollinger_percent_b" not in short
print("indicators OK:", {k: ind[k] for k in ("macd", "macd_histogram",
      "bollinger_percent_b", "bollinger_width_pct", "atr_14", "atr_pct_of_price",
      "relative_volume", "forecast_price_5d", "forecast_change_5d_pct",
      "forecast_trend_r2")})

# ---- feeling-lucky discovery ranking ---------------------------------------
steady_up = [100 * (1.003**i) for i in range(70)]
flat = [100.0 for _ in range(70)]
down = [100 * (0.997**i) for i in range(70)]
caps = {"UP": 2_000_000_000, "FLAT": 1_000_000_001, "DOWN": 3_000_000_000}
ranked = rank_candidates({"UP": steady_up, "FLAT": flat, "DOWN": down}, caps, "short_term", 3)
assert [item["ticker"] for item in ranked] == ["UP", "FLAT", "DOWN"], ranked
assert rank_candidates({"SMALL": steady_up}, {"SMALL": 1_000_000_000}, "long_term", 5) == []
assert rank_candidates({"BIG": steady_up}, {"BIG": 50_000_000_000}, "long_term", 5) == []
assert rank_candidates({"SHORT": [1.0] * 20}, {"SHORT": 2_000_000_000}, "long_term", 5) == []
print("discovery ranking OK:", [item["ticker"] for item in ranked])

# ---- discovery provider cache ----------------------------------------------
cache_calls = 0
original_download = discovery._download_candidates
discovery._candidate_cache = None


def fake_candidates():
    global cache_calls
    cache_calls += 1
    return ({ticker: steady_up for ticker in ("A", "B", "C", "D", "E")},
            {ticker: 2_000_000_000 for ticker in ("A", "B", "C", "D", "E")})


try:
    discovery._download_candidates = fake_candidates
    first, first_hit = discovery._cached_candidates()
    second, second_hit = discovery._cached_candidates()
    assert first == second and not first_hit and second_hit and cache_calls == 1
    discovery._candidate_cache = (
        discovery._candidate_cache[0] - discovery.CACHE_TTL_SECONDS - 1,
        discovery._candidate_cache[1],
    )
    _, expired_hit = discovery._cached_candidates()
    assert not expired_hit and cache_calls == 2
finally:
    discovery._download_candidates = original_download
    discovery._candidate_cache = None
print("discovery cache OK: one-hour hit and expiry refresh")

# ---- rebuttal mocks (deterministic, offline) --------------------------------
from app.agents import bear, bull, forecast as forecast_agent  # noqa: E402

_round = {"score": 0.8, "summary": "own argument"}
_opp = {"score": 0.7, "summary": "opponent argument"}
_bull_rebuttal = bull.mock("NVDA", {"own_round_1": _round, "opponent_round_1": _opp}, rebuttal=True)
_bear_rebuttal = bear.mock("NVDA", {"own_round_1": _opp, "opponent_round_1": _round}, rebuttal=True)
assert 0.0 <= _bull_rebuttal["score"] <= 1.0 and "Rebuttal" in _bull_rebuttal["summary"]
assert 0.0 <= _bear_rebuttal["score"] <= 1.0 and "Rebuttal" in _bear_rebuttal["summary"]
print("rebuttal mocks OK:", _bull_rebuttal["score"], _bear_rebuttal["score"])

# ---- forecast analyst mock ---------------------------------------------------
_fc_payload = {
    "price": 100.0,
    "history_days": 60,
    "timegpt_forecast": {"forecast_change_5d_pct": -4.0, "forecast_method": "timegpt-1"},
    "local_trend_forecast": {"forecast_change_5d_pct": 1.0, "forecast_trend_r2": 0.9,
                             "forecast_method": "log_linear_trend"},
    "trend_context": {"volatility_annualized_pct": 20.0},
}
_fc = forecast_agent.mock("NVDA", _fc_payload)
assert _fc["signal"] == "bearish" and _fc["confidence"] >= 0.5, _fc  # -4% clears ~2.8% noise
_fc_noisy = forecast_agent.mock("NVDA", {**_fc_payload,
                                         "timegpt_forecast": {"forecast_change_5d_pct": -1.0,
                                                              "forecast_method": "timegpt-1"}})
assert _fc_noisy["signal"] == "neutral", _fc_noisy  # -1% is inside the noise band
_fc_weak = forecast_agent.mock("NVDA", {"timegpt_forecast": None,
                                        "local_trend_forecast": {"forecast_change_5d_pct": 3.0,
                                                                 "forecast_trend_r2": 0.1,
                                                                 "forecast_method": "log_linear_trend"},
                                        "trend_context": {"volatility_annualized_pct": 20.0}})
assert _fc_weak["signal"] == "neutral" and "unusable" in _fc_weak["summary"], _fc_weak
print("forecast mock OK:", _fc["signal"], _fc["confidence"])

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
