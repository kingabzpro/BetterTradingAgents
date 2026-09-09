"""Offline checks for the decision brief and trust state (ROADMAP P0.1).

Run: PYTHONPATH=. uv run python scripts/check_decision_brief.py
No network: the e2e injects an in-memory MarketData snapshot (the same seam the
backtester uses), so the whole pipeline runs in mock mode hermetically; the
risk gate is patched in one scenario to force the BUY -> HOLD downgrade the
acceptance criteria describe.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Isolated DB + no provider keys before app.config is imported.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "brief_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""

from app.agents import manager as manager_agent  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import AgentResult, StockAnalysis  # noqa: E402
from app.quality import STALE_AFTER_HOURS, age_hours, build, stale_after_hours  # noqa: E402
from app.runs import Run, RunStore  # noqa: E402
from app.tools.market_data import MarketData  # noqa: E402
from app.workflow import analyze_ticker  # noqa: E402
from app import risk  # noqa: E402

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

# ---- stale thresholds are per outlook, not one global number -------------------
assert STALE_AFTER_HOURS == {"day_trade": 24.0, "short_term": 168.0, "long_term": 1080.0}
assert stale_after_hours("day_trade") < stale_after_hours("short_term") < stale_after_hours("long_term")
assert stale_after_hours("nonsense") == stale_after_hours("short_term")  # coerced, not crashed
assert age_hours("2026-09-08T09:00:00+00:00", NOW) == 3.0
assert age_hours("", NOW) is None
assert age_hours("garbage", NOW) is None
print("staleness OK: per-outlook thresholds, tolerant parsing")


def results(**overrides):
    base = {key: AgentResult(agent=key, signal="neutral") for key in
            ("technical", "fundamental", "news", "sentiment", "forecast")}
    base.update(overrides)
    return base


ALL = ("technical", "fundamental", "news", "sentiment", "forecast")

# ---- fresh vs stale vs unknown -------------------------------------------------
fresh = build("2026-09-08T11:00:00+00:00", "day_trade", ALL,
              results(), {"prices": "yfinance"}, now=NOW)
assert fresh.stale is False and fresh.stale_reason == ""
assert fresh.age_hours == 1.0 and fresh.stale_after_hours == 24.0

stale_day = build("2026-09-05T11:00:00+00:00", "day_trade", ALL,
                  results(), {}, now=NOW)
assert stale_day.stale is True and "Day trading" in stale_day.stale_reason
assert "3d" not in stale_day.stale_reason  # reason states hours, plainly

same_stamp = build("2026-09-05T11:00:00+00:00", "long_term", ALL,
                   results(), {}, now=NOW)
assert same_stamp.stale is False, "the same age must not be stale for every outlook"

unknown = build("", "short_term", ALL, results(), {}, now=NOW)
assert unknown.stale is False and unknown.stale_reason == "data timestamp unavailable"
assert unknown.age_hours is None
print("stale logic OK: same stamp, different verdicts by outlook; unknown stays unknown")

# ---- coverage: expected / available / failed / skipped --------------------------
full = build("2026-09-08T11:00:00+00:00", "short_term", ALL,
             results(), {}, now=NOW)
assert full.expected_analysts == list(ALL)
assert full.available_analysts == list(ALL) and full.failed_analysts == []

partial = build("2026-09-08T11:00:00+00:00", "short_term", ALL,
                results(fundamental=None, sentiment=None), {}, now=NOW)
assert partial.available_analysts == ["technical", "news", "forecast"]
assert partial.failed_analysts == ["fundamental", "sentiment"]

fast = build("2026-09-08T11:00:00+00:00", "short_term",
             ("technical", "news"), results(), {}, now=NOW)
assert fast.expected_analysts == ["technical", "news"]
assert fast.skipped_analysts == ["fundamental", "sentiment", "forecast"]
print("coverage OK: failed is a breakdown of expected, skipped is a choice")

# ---- provider fallbacks only fire when the provider was configured -------------
no_keys = build("2026-09-08T11:00:00+00:00", "short_term", ALL,
                results(), {"forecast": "local", "fundamentals": "yfinance",
                            "social": "none"}, now=NOW)
assert no_keys.provider_fallbacks == ["social posts: none retrieved"], no_keys.provider_fallbacks

settings.nixtla_api_key = "test-key"
settings.finnhub_api_key = "test-key"
configured = build("2026-09-08T11:00:00+00:00", "short_term", ALL,
                   results(), {"forecast": "local", "fundamentals": "yfinance",
                               "social": "olostep"}, now=NOW)
assert configured.provider_fallbacks == [
    "forecast: local model (TimeGPT unavailable)",
    "fundamentals: yfinance (finnhub returned nothing)",
]
settings.nixtla_api_key = ""
settings.finnhub_api_key = ""
print("fallbacks OK: degraded inputs named, unconfigured providers not flagged")

# ---- manager output: conditions parsed, clipped, and mocked ---------------------
parsed = manager_agent.to_manager_result(
    {"decision": "buy", "confidence": 0.8, "summary": "s",
     "would_upgrade_if": "u" * 900, "would_downgrade_if": "d"}, "NVDA"
)
assert parsed.decision == "BUY"
assert len(parsed.would_upgrade_if) <= 301
assert parsed.would_downgrade_if == "d"
mocked = manager_agent.mock("NVDA", {"bull": {"confidence": 0.8}, "bear": {"confidence": 0.4}})
assert mocked["decision"] == "BUY"
assert mocked["would_upgrade_if"] and mocked["would_downgrade_if"]
print("manager OK: conditions parsed, clipped, mocked")

# ---- old persisted runs still parse with safe defaults -------------------------
legacy = StockAnalysis.model_validate({
    "ticker": "MSFT", "decision": "HOLD", "confidence": 0.4,
    "as_of": "2026-01-02T10:00:00+00:00",
})
assert legacy.manager_decision is None and legacy.manager_confidence is None
assert legacy.would_upgrade_if == "" and legacy.would_downgrade_if == ""
assert legacy.data_quality.expected_analysts == [] and legacy.data_quality.stale is False
roundtrip = StockAnalysis.model_validate(legacy.model_dump())
assert roundtrip.manager_decision is None
print("legacy parse OK: pre-P0.1 rows load with safe defaults")


# ---- hermetic e2e ---------------------------------------------------------------
def snapshot(ticker: str) -> MarketData:
    closes = [100.0 + i * 0.4 for i in range(130)]  # steady uptrend
    return MarketData(
        ticker=ticker,
        price=closes[-1],
        company_name="Test Corp",
        closes=closes,
        highs=[c * 1.01 for c in closes],
        lows=[c * 0.99 for c in closes],
        volumes=[1_000_000.0] * len(closes),
        fundamentals={"pe_ratio_ttm": 20.0},
        news=[{"title": "Steady growth", "source": "finnhub", "published": "",
               "url": "https://example.com/g"}],
        social=[],
        sources={"prices": "yfinance", "fundamentals": "yfinance",
                 "news": "yfinance", "social": "none", "forecast": "local"},
        as_of=datetime.now(timezone.utc).isoformat(),
    )


async def e2e():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    # 1) Plain mock run: manager call preserved, data_quality attached.
    result = await asyncio.wait_for(
        analyze_ticker("NVDA", emit, market_data=snapshot("NVDA"),
                       live_context=False),
        timeout=90,
    )
    assert result.error is None, result.error
    assert result.manager_decision == result.decision, (
        "without a gate change both calls must agree"
    )
    assert result.manager_confidence is not None
    assert result.would_upgrade_if and result.would_downgrade_if
    dq = result.data_quality
    assert dq.expected_analysts == list(ALL)
    assert dq.available_analysts == list(ALL) and dq.failed_analysts == []
    assert dq.skipped_analysts == []
    assert dq.stale is False and dq.age_hours is not None
    assert "social posts: none retrieved" in dq.provider_fallbacks
    print("e2e (no gate change) OK:", result.decision,
          "| coverage", f"{len(dq.available_analysts)}/{len(dq.expected_analysts)}")

    # 2) Risk gate downgrades the manager's BUY -> both calls must survive.
    forced_flag = "downgraded BUY to HOLD: exposure cap test"
    real_evaluate, real_mock = risk.evaluate, manager_agent.mock

    def fake_evaluate(decision, confidence, ticker, **_):
        return "HOLD", confidence, None, [forced_flag]

    def buy_mock(ticker, payload):
        return {"ticker": ticker, "decision": "BUY", "confidence": 0.9,
                "summary": "bull case wins", "bull_case": "b", "bear_case": "r",
                "would_upgrade_if": "stronger", "would_downgrade_if": "weaker"}

    risk.evaluate = fake_evaluate
    manager_agent.mock = buy_mock
    try:
        gated = await asyncio.wait_for(
            analyze_ticker("AMD", emit, market_data=snapshot("AMD"),
                           live_context=False),
            timeout=90,
        )
    finally:
        risk.evaluate = real_evaluate
        manager_agent.mock = real_mock
    assert gated.manager_decision == "BUY" and gated.decision == "HOLD"
    assert gated.manager_confidence == 0.9
    assert forced_flag in gated.risk_flags
    assert gated.would_upgrade_if == "stronger"
    print("e2e (gate downgrade) OK: Manager BUY -> Final HOLD, flag preserved")

    # 3) Cached replays keep the original as_of and are marked cached.
    run = Run(["NVDA"])
    await RunStore._emit_cached(run, result)
    completed = [e for e in run.events if e["type"] == "ticker_completed"]
    assert completed and completed[0]["cached"] is True
    assert completed[0]["analysis"]["as_of"] == result.as_of
    assert completed[0]["duration_s"] == 0.0
    print("cached replay OK: cached flag + original as_of, zero duration")

asyncio.run(e2e())

# ---- UI hooks: trust columns, split, gate line, conditions, reading order -------
root = Path(__file__).resolve().parents[1]
js = "\n".join(path.read_text(encoding="utf-8") for path in sorted((root / "static" / "js").glob("*.js")))
html = (root / "static" / "index.html").read_text(encoding="utf-8")
css = "\n".join(path.read_text(encoding="utf-8") for path in sorted((root / "static" / "css").glob("*.css")))

for token in ("signalSplit", "splitLabel", "gateLine", "gateChanged", "downgradeFlag",
              "staleInfo", "coverageInfo", "dataAgeHours", "risk-adjusted",
              "Manager: ", "Analyst coverage", "Conditions for a different call",
              "entry.cached", "Data age", "Horizon"):
    assert token in js, f"app.js missing {token}"
assert 'type="module" src="/static/js/app.js' in html
for token in (".gate-banner", ".gate-chip", ".gate-line", ".manager-conditions"):
    assert token in css, f"style.css missing {token}"

# The expanded brief follows one reading order: summary + risk changes (brief),
# then debate, evidence, sources, prior outcomes (detail).
card_body = js[js.index("function renderResultCard"):js.index("function toggleResult")]
template = card_body[card_body.index("card.innerHTML = `"):]
order = [template.index("gate-banner"), template.index("manager-conclusion"),
         template.index("${conditions}"), template.index("risk-flags"),
         template.index("debate-title-"), template.index("evidence-title-"),
         template.index("sources-title-"), template.index("renderTrackRecord")]
assert order == sorted(order), f"reading order broken: {order}"
print("UI hooks OK: trust columns, gate line, conditions, cached, reading order")

print("ALL DECISION BRIEF CHECKS PASSED")
