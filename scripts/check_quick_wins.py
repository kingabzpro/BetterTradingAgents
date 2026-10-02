"""One-off sanity checks for the quick-win changes. Run: uv run python scripts/check_quick_wins.py"""

import asyncio
import os
import random
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
steady_up = [100 * (1.003**i) for i in range(200)]
flat = [100.0 for _ in range(200)]
down = [100 * (0.997**i) for i in range(200)]
caps = {"UP": 2_000_000_000, "FLAT": 1_000_000_001, "DOWN": 3_000_000_000}
ranked = rank_candidates({"UP": steady_up, "FLAT": flat, "DOWN": down}, caps, "short_term", 3)
assert [item["ticker"] for item in ranked] == ["UP", "FLAT", "DOWN"], ranked
assert rank_candidates({"SMALL": steady_up}, {"SMALL": 1_000_000_000}, "long_term", 5) == []
assert rank_candidates({"BIG": steady_up}, {"BIG": 50_000_000_000}, "long_term", 5) == []
assert rank_candidates({"SHORT": [1.0] * 20}, {"SHORT": 2_000_000_000}, "long_term", 5) == []

# Frog in the pan (Da et al. 2014): a smooth climb outranks a jumpy path to
# the same price - steady 0.3%/day vs the same total return in decade-block
# jumps (the jumpy path even ends 0.3% higher, biasing against the assertion).
jumpy = [100 * ((1.003 ** 10) ** (i // 10 + 1)) for i in range(200)]
assert abs(jumpy[-1] - steady_up[-1]) < 1.0, (jumpy[-1], steady_up[-1])
frog = rank_candidates(
    {"SMOOTH": steady_up, "JUMPY": jumpy},
    {"SMOOTH": 2_000_000_000, "JUMPY": 2_000_000_000},
    "short_term",
    2,
)
assert [item["ticker"] for item in frog] == ["SMOOTH", "JUMPY"], frog

# Last-month blow-off (Jegadeesh 1990 reversal): a TARS-style +50% month on a
# flat base ranks far below a steady climber that reaches the same price.
climber = [100 * (1.0025**i) for i in range(200)]
blowoff = [100 * (1.0005**i) for i in range(179)]
final_jump = climber[-1] / blowoff[-1]
blowoff += [blowoff[-1] * final_jump ** ((k + 1) / 21) for k in range(21)]
assert abs(blowoff[-1] - climber[-1]) < 1e-6
spike = rank_candidates(
    {"CLIMB": climber, "BLOWOFF": blowoff},
    {"CLIMB": 2_000_000_000, "BLOWOFF": 2_000_000_000},
    "short_term",
    2,
)
assert [item["ticker"] for item in spike] == ["CLIMB", "BLOWOFF"], spike
print("discovery ranking OK:", [item["ticker"] for item in ranked],
      "| frog-in-the-pan:", [item["ticker"] for item in frog],
      "| blow-off demoted:", [item["ticker"] for item in spike])

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

# ---- debate judge mock (deterministic, offline) ------------------------------
from app.agents import bear, bull, forecast as forecast_agent, judge  # noqa: E402

_verdict = judge.mock("NVDA", {"bull_round_1": {"score": 0.8, "summary": "bull"}, "bear_round_1": {"score": 0.7, "summary": "bear"}})
assert 0.0 <= _verdict["bull_strength"] <= 1.0 and 0.0 <= _verdict["bear_strength"] <= 1.0
assert _verdict["signal"] in ("bullish", "bearish", "neutral")
assert 0.0 <= _verdict["confidence"] <= 1.0
assert "[mock] Judge:" in _verdict["summary"]
assert judge.to_result(_verdict, "NVDA").signal == _verdict["signal"]
print("debate judge mocks OK:", _verdict["bull_strength"], _verdict["bear_strength"], _verdict["signal"])

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

# ---- workflow imports (catches syntax / import errors everywhere) ----------
import app.main  # noqa: E402, F401

print("app.main import OK")
print("ALL CHECKS PASSED")
