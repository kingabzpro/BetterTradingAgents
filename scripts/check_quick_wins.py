"""One-off sanity checks for the quick-win changes. Run: uv run python scripts/check_quick_wins.py"""

import os
import random
import tempfile
from math import exp
from pathlib import Path

# Isolated DB before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "portfolio_test.db"
os.environ["DB_PATH"] = str(_TMP)

from app.tools.indicators import compute_indicators, historical_forecast, macd  # noqa: E402
from app.tools.market_data import _normalize_bako_forecast, _news_items_from_rows  # noqa: E402
from app.outlook import horizon_days as outlook_horizon  # noqa: E402
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
assert forecast["forecast_change_5d_pct"] > 0
assert len(forecast["forecast_path"]) == 5 and forecast["forecast_path"][-1] == forecast["forecast_price_5d"]
# A perfect geometric series has constant returns, so return-prediction skill
# is degenerate there. Skill shows on return structure: an accelerating trend
# (quadratic in log price) makes each future return an exact linear function
# of the last few returns at every point of the series, so a correct fit
# scores ~1 out-of-sample. Real equities sit near zero, which is why the app
# discounts non-positive skill instead of expecting it.
accelerating = [100 * exp(0.003 * i + 0.00002 * i * i) for i in range(130)]
assert historical_forecast(accelerating)["forecast_trend_r2"] > 0.9
assert historical_forecast([1, 2, 3]) is None

# Bako: rows are {date, price} calendar days from the prediction run date;
# trading-day step k reads the row at calendar offset round(k * 365/252), so
# a 5-day call reads row 7. Bad payloads fall through to the next provider.
bako = _normalize_bako_forecast(
    {
        "symbol": "NVDA",
        "data": [{"date": f"2026-10-{2 + i}", "price": 100 + i} for i in range(8)],
    },
    100,
    5,
)
assert bako and bako["forecast_price_5d"] == 107
assert bako["forecast_change_5d_pct"] == 7 and bako["forecast_method"] == "bako-timesfm"
assert bako["forecast_horizon_days"] == 5
assert bako["forecast_path"] == [101, 103, 104, 106, 107]
assert _normalize_bako_forecast({}, 100, 5) is None
assert _normalize_bako_forecast({"data": []}, 100, 5) is None
assert _normalize_bako_forecast({"data": [{"price": 101}]}, 100, 5) is None  # too short
assert _normalize_bako_forecast({"data": [{"date": "x"}] * 5}, 100, 5) is None  # no price
print("Bako normalizer OK: calendar-mapped rows, path, method tag, short/gap payloads")

# Generic search-row mapper (Tavily "content" / Firecrawl "description"):
# rows without a title or a safe http(s) URL are dropped.
_rows = [
    {"title": "Nvidia news", "url": "https://www.reuters.com/article/nvidia", "content": "chip demand" * 40},
    {"title": "No url", "url": "", "content": "x"},
    {"title": "Javascript url", "url": "javascript:alert(1)", "content": "x"},
    {"url": "https://example.com", "content": "no title"},
]
_mapped = _news_items_from_rows(_rows, "content")
assert len(_mapped) == 1 and _mapped[0]["source"] == "reuters.com", _mapped
assert _mapped[0]["summary"] == ("chip demand" * 40)[:300]  # capped at 300 chars
_fc_rows = [{"title": "AMD news", "url": "https://firecrawl.dev/x", "description": "cpus"}]
assert _news_items_from_rows(_fc_rows, "description")[0]["summary"] == "cpus"
print("search-row mapper OK: tavily/firecrawl shapes, unsafe rows dropped, summary capped")

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
from app.agents import forecast as forecast_agent, judge  # noqa: E402

_verdict = judge.mock("NVDA", {"bull_round_1": {"score": 0.8, "summary": "bull"}, "bear_round_1": {"score": 0.7, "summary": "bear"}})
assert 0.0 <= _verdict["bull_strength"] <= 1.0 and 0.0 <= _verdict["bear_strength"] <= 1.0
assert _verdict["signal"] in ("bullish", "bearish", "neutral")
assert 0.0 <= _verdict["confidence"] <= 1.0
assert "[mock] Judge:" in _verdict["summary"]
assert judge.to_result(_verdict, "NVDA").signal == _verdict["signal"]
print("debate judge mocks OK:", _verdict["bull_strength"], _verdict["bear_strength"], _verdict["signal"])

# ---- market analyst mock (deterministic, offline) ----------------------------
from app.agents import market  # noqa: E402

_regime = market.mock(
    "NVDA",
    {
        "spx": {"last": 5900.0, "change_5d_pct": 1.2, "change_21d_pct": 3.0, "above_sma20": True},
        "nasdaq": {"last": 19200.0, "change_5d_pct": -0.4, "change_21d_pct": 2.1, "above_sma20": True},
        "vix": {"last": 14.2, "change_5d_pct": -3.1},
        "rates_10y": {"last": 4.2, "change_5d_pct": 0.9},
    },
)
assert _regime["signal"] in ("bullish", "bearish", "neutral")
assert 0.0 <= _regime["confidence"] <= 1.0
assert "[mock] S&P 5d" in _regime["summary"]
assert market.to_result(_regime, "NVDA").signal == _regime["signal"]
assert market.to_result({}, "NVDA").signal == "neutral"
print("market analyst mocks OK:", _regime["signal"], _regime["confidence"])

# ---- forecast analyst mock ---------------------------------------------------
_fc_payload = {
    "price": 100.0,
    "history_days": 60,
    "hosted_forecast": {"forecast_change_5d_pct": -4.0, "forecast_method": "bako-timesfm"},
    "local_trend_forecast": {"forecast_change_5d_pct": 1.0, "forecast_trend_r2": 0.9,
                             "forecast_method": "log_linear_trend"},
    "trend_context": {"volatility_annualized_pct": 20.0},
}
_fc = forecast_agent.mock("NVDA", _fc_payload)
assert _fc["signal"] == "bearish" and _fc["confidence"] >= 0.5, _fc  # -4% clears ~2.8% noise
_fc_noisy = forecast_agent.mock("NVDA", {**_fc_payload,
                                         "hosted_forecast": {"forecast_change_5d_pct": -1.0,
                                                              "forecast_method": "bako-timesfm"}})
assert _fc_noisy["signal"] == "neutral", _fc_noisy  # -1% is inside the noise band
_fc_weak = forecast_agent.mock("NVDA", {"hosted_forecast": None,
                                        "local_trend_forecast": {"forecast_change_5d_pct": 3.0,
                                                                 "forecast_trend_r2": 0.1,
                                                                 "forecast_method": "log_linear_trend"},
                                        "trend_context": {"volatility_annualized_pct": 20.0}})
assert _fc_weak["signal"] == "neutral" and "unusable" in _fc_weak["summary"], _fc_weak
print("forecast mock OK:", _fc["signal"], _fc["confidence"])

# The analyst names the actual provider in its prompt, never a hardcoded one.
assert forecast_agent._hosted_provider({"hosted_forecast": {"forecast_method": "bako-timesfm"}}) == "Bako (TimesFM)"
assert forecast_agent._hosted_provider({"hosted_forecast": {"forecast_method": "timegpt-1"}}) == "Nixtla TimeGPT"
assert forecast_agent._hosted_provider({}) == "the configured hosted provider"
print("forecast provider label OK")

# Outlook horizons: day trade grades the next session, short term five days,
# long term about a quarter; the forecast mock scales its noise band to match.
assert outlook_horizon("day_trade") == 1 and outlook_horizon("short_term") == 5
assert outlook_horizon("long_term") == 63 and outlook_horizon("nonsense") == 5
_daily = historical_forecast(up, 1)
_weekly = historical_forecast(up, 5)
assert _daily["forecast_price_5d"] < _weekly["forecast_price_5d"]  # uptrend projects higher later
_fc_day = forecast_agent.mock("NVDA", {**_fc_payload, "horizon_days": 1,
                                       "hosted_forecast": {"forecast_change_5d_pct": -2.0,
                                                            "forecast_method": "bako-timesfm"}})
assert _fc_day["signal"] == "bearish", _fc_day  # -2% clears the ~1.3% 1-day band
_fc_week = forecast_agent.mock("NVDA", {**_fc_payload,
                                        "hosted_forecast": {"forecast_change_5d_pct": -2.0,
                                                             "forecast_method": "bako-timesfm"}})
assert _fc_week["signal"] == "neutral", _fc_week  # the same move is noise over 5 days
print("outlook horizons OK: 1/5/63 days drive the forecast and its noise band")

# ---- workflow imports (catches syntax / import errors everywhere) ----------
import app.main  # noqa: E402, F401

print("app.main import OK")

# The `uv run app` welcome banner: the ACTUAL port's link, docs, commands.
from app.cli import WIKI_URL, app_url, pick_port, startup_banner  # noqa: E402

_banner = startup_banner(port=8123)
assert app_url(8123) in _banner and WIKI_URL in _banner, _banner
assert "uv run setup" in _banner and "uv run test" in _banner
import re as _re  # noqa: E402

_plain = _re.sub(r"\x1b\[[0-9;]*m", "", _banner)
_bars = [len(line) for line in _plain.splitlines() if "───" in line]
assert _bars and max(len(line) for line in _plain.splitlines()) <= max(_bars), \
    "banner lines must not be wider than the frame bars"
print("startup banner OK (port link, wiki link, commands, bars cover all text)")

# A busy port is skipped: pick_port returns a later free port in range.
import socket as _socket  # noqa: E402

_probe = _socket.socket()
_probe.bind(("127.0.0.1", 0))
_busy_port = _probe.getsockname()[1]
_probe.listen(1)
_chosen = pick_port(_busy_port)
_probe.close()
assert _busy_port < _chosen <= _busy_port + 20, (_busy_port, _chosen)
assert app_url(_chosen).endswith(str(_chosen))
print(f"port pick OK (busy {_busy_port} -> {_chosen})")

# The Agents SDK tracer stays disabled (config.py sets the env before any SDK
# import), so no trace-export warnings or API calls ever happen.
import os as _os  # noqa: E402

assert _os.environ.get("OPENAI_AGENTS_DISABLE_TRACING") == "1"
print("agents sdk tracing disabled OK")

print("ALL CHECKS PASSED")
