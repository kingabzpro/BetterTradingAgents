"""Offline checks for confidence calibration (ROADMAP P1.1).

Seeds a temporary decisions database with known outcomes, then verifies the
bucket math, the minimum-sample gate, HOLD treatment (no invented win rate),
the manager-probability Brier score, the per-analyst scorecard join,
provenance recording, and the /api/calibration endpoint. No LLM and no
network access.

Run: uv run python -m scripts.check_calibration
"""

import asyncio
import json
import math
import os
from datetime import date
from pathlib import Path
import sqlite3
import tempfile

# Small gate so the threshold math is easy to verify by hand.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "calibration_test.db")
os.environ["CALIBRATION_MIN_OBSERVATIONS"] = "4"

import httpx  # noqa: E402

from app import calibration, memory, run_history  # noqa: E402
from app.agents import manager as manager_agent  # noqa: E402
from app.main import app  # noqa: E402
from app.models import StockAnalysis  # noqa: E402

SCOPE = {"outlook": "short_term", "depth": "medium", "model": "model-m",
         "policy_version": calibration.DECISION_POLICY_VERSION}


def seed(rows: list[dict]) -> None:
    base = {"ticker": "TEST", "date": "2026-01-10", "confidence": 0.72,
            "decision": "BUY", "manager_probability": None, **SCOPE}
    with sqlite3.connect(os.environ["DB_PATH"]) as connection:
        for row in rows:
            merged = {**base, **row}
            connection.execute(
                """
                INSERT INTO decisions (
                    ticker, date, decision, confidence, price_at_decision,
                    outlook, depth, model, policy_version, manager_probability,
                    mature, realized_return_pct, spy_return_pct, alpha_vs_spy_pct, window_days
                ) VALUES (
                    :ticker, :date, :decision, :confidence, 100,
                    :outlook, :depth, :model, :policy_version, :manager_probability,
                    1, :realized_return_pct, :spy_return_pct, :alpha_vs_spy_pct, 21
                )
                """,
                merged,
            )


async def checks() -> None:
    await memory.init()
    # BUY strong bucket: 6 graded alphas (hit = alpha > 0), 1 ungradable row
    # (no SPY alpha) that must stay out of every rate.
    buy_alphas = [3.0, 1.0, -0.5, 2.0, -1.0, 0.5]
    rows = [
        {"alpha_vs_spy_pct": a, "realized_return_pct": a + 1.0, "spy_return_pct": 1.0}
        for a in buy_alphas
    ]
    rows.append({"alpha_vs_spy_pct": None, "realized_return_pct": 4.0, "spy_return_pct": None})
    # BUY moderate bucket: below the gate.
    rows += [
        {"confidence": 0.55, "alpha_vs_spy_pct": 1.0, "realized_return_pct": 1.5, "spy_return_pct": 0.5}
        for _ in range(3)
    ]
    # HOLD bucket: realized returns decide missed upside / avoided downside.
    for realized in (3.0, 2.5, -3.0, 0.0):
        rows.append({
            "decision": "HOLD", "confidence": 0.6, "alpha_vs_spy_pct": None,
            "realized_return_pct": realized, "spy_return_pct": 0.0,
        })
    # SELL bucket with manager probabilities: success = alpha < 0.
    for alpha, probability in ((-2.0, 0.70), (1.0, 0.60), (-3.0, 0.90), (-0.5, 0.55)):
        rows.append({
            "decision": "SELL", "confidence": 0.8, "alpha_vs_spy_pct": alpha,
            "realized_return_pct": alpha - 1.0, "spy_return_pct": -1.0,
            "manager_probability": probability,
        })
    # Rows recorded before provenance existed: separate scope, never pooled.
    for alpha in (2.0, 2.5):
        rows.append({
            "outlook": "", "depth": "", "model": "", "policy_version": "",
            "alpha_vs_spy_pct": alpha, "realized_return_pct": alpha, "spy_return_pct": 0.0,
        })
    seed(rows)

    # Bucket edges mirror the UI labels.
    assert calibration.confidence_bucket(0.49) == "low"
    assert calibration.confidence_bucket(0.50) == "moderate"
    assert calibration.confidence_bucket(0.69) == "moderate"
    assert calibration.confidence_bucket(0.70) == "strong"

    # BUY strong: rates over the 6 gradable rows only.
    record = calibration.track_record("BUY", 0.72, "short_term", "medium")
    assert record["available"] and record["n"] == 6, record
    assert math.isclose(record["directional_hit_rate"], 4 / 6)
    assert math.isclose(record["positive_alpha_rate"], 4 / 6)
    assert math.isclose(record["mean_alpha_pct"], 5 / 6, abs_tol=1e-9)
    assert math.isclose(record["median_alpha_pct"], 0.75)
    assert record["models_pooled"] == 1

    # Below the gate: explicit unavailable, sample size still reported.
    small = calibration.track_record("BUY", 0.55, "short_term", "medium")
    assert not small["available"] and small["n"] == 3 and small["min_observations"] == 4

    # HOLD: missed upside / avoided downside, no invented win rate.
    hold = calibration.track_record("HOLD", 0.6, "short_term", "medium")
    assert hold["available"] and hold["n"] == 4
    assert math.isclose(hold["missed_upside_rate"], 2 / 4)
    assert math.isclose(hold["avoided_downside_rate"], 1 / 4)
    assert "directional_hit_rate" not in hold

    # SELL: success is direction-aware (alpha < 0), unlike positive alpha.
    sell = calibration.track_record("SELL", 0.8, "short_term", "medium")
    assert sell["available"] and sell["n"] == 4
    assert math.isclose(sell["directional_hit_rate"], 3 / 4)
    assert math.isclose(sell["positive_alpha_rate"], 1 / 4)

    # Unversioned rows live in their own scope and never leak into lookups.
    legacy = calibration.track_record("BUY", 0.72, "short_term", "medium")
    assert legacy["n"] == 6, "legacy rows must not pool into the current scope"

    # Report: unavailable lines, scope labels, Brier, HOLD note - SQLite only.
    report = calibration.build_report()
    assert "Track record unavailable (n=3 < 4)" in report
    assert "short_term / medium / model-m" in report
    assert "unknown outlook / unknown depth / unknown model / unversioned" in report
    assert "no win rate is invented" in report.lower()
    expected_brier = (0.09 + 0.36 + 0.01 + 0.2025) / 4  # (0.7,1) (0.6,0) (0.9,1) (0.55,1)
    assert f"Brier score: {expected_brier:.4f}" in report

    # Recording carries provenance and the manager's pre-gate fields.
    analysis = StockAnalysis(
        ticker="REC", decision="HOLD", confidence=0.4, price=10.0,
        manager_decision="BUY", manager_confidence=0.72, manager_probability=0.66,
    )
    await memory.record_decision("run_rec", analysis, decision_date="2026-02-02", provenance={
        **SCOPE, "success_event": calibration.success_event(),
    })
    with sqlite3.connect(os.environ["DB_PATH"]) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM decisions WHERE ticker = 'REC'"
        ).fetchone()
    assert row["outlook"] == "short_term" and row["depth"] == "medium"
    assert row["model"] == "model-m" and row["policy_version"] == calibration.DECISION_POLICY_VERSION
    assert row["manager_decision"] == "BUY" and row["manager_confidence"] == 0.72
    assert row["manager_probability"] == 0.66
    assert calibration.success_event() in row["success_event"]

    # Manager parsing: probability is clamped, dropped for HOLD, and never
    # confused with evidence-strength confidence.
    parsed = manager_agent.to_manager_result(
        {"decision": "buy", "confidence": 0.8, "probability_beat_spy": "0.62"}, "T"
    )
    assert parsed.decision == "BUY" and parsed.probability_beat_spy == 0.62
    assert manager_agent.to_manager_result(
        {"decision": "HOLD", "probability_beat_spy": 0.9}, "T"
    ).probability_beat_spy is None
    assert manager_agent.to_manager_result(
        {"decision": "SELL", "probability_beat_spy": 1.4}, "T"
    ).probability_beat_spy == 1.0
    mock_buy = manager_agent.mock("T", {"bull": {"confidence": 0.8}, "bear": {"confidence": 0.5}})
    assert mock_buy["decision"] == "BUY" and mock_buy["probability_beat_spy"] == 0.8
    mock_hold = manager_agent.mock("T", {"bull": {"confidence": 0.5}, "bear": {"confidence": 0.55}})
    assert mock_hold["decision"] == "HOLD" and mock_hold["probability_beat_spy"] is None

    # Item 11: target weight parsing is clamped, dropped for HOLD, and never
    # confused with confidence; the mock manager emits a deterministic weight.
    parsed_w = manager_agent.to_manager_result(
        {"decision": "BUY", "target_weight_pct": "7.5"}, "T"
    )
    assert parsed_w.target_weight_pct == 7.5
    assert manager_agent.to_manager_result(
        {"decision": "HOLD", "target_weight_pct": 7.5}, "T"
    ).target_weight_pct is None
    assert manager_agent.to_manager_result(
        {"decision": "BUY", "target_weight_pct": 250}, "T"
    ).target_weight_pct == 100.0
    assert manager_agent.to_manager_result(
        {"decision": "BUY", "target_weight_pct": "a lot"}, "T"
    ).target_weight_pct is None
    assert manager_agent.to_manager_result({"decision": "BUY"}, "T").target_weight_pct is None
    assert 2.0 <= mock_buy["target_weight_pct"] <= 10.0, mock_buy
    assert mock_hold["target_weight_pct"] is None
    print("manager target weight OK:", parsed_w.target_weight_pct)

    # Item 19: calibration-driven sizing. Four mature BUY rows in the
    # 0.80-0.89 band, two winners: observed 50% vs predicted 85% -> 0.59.
    # Below the minimum sample, or an empty band, the curve stays out of the
    # way (1.0, no note); None never scales.
    seed(
        [
            {
                "decision": "BUY",
                "confidence": 0.65,
                "outlook": "long_term",
                "manager_probability": 0.85,
                "alpha_vs_spy_pct": alpha,
                "realized_return_pct": alpha + 1.0,
                "spy_return_pct": 1.0,
            }
            for alpha in (1.0, 1.0, -1.0, -1.0)
        ]
    )
    multiplier, note = calibration.sizing_multiplier_for(0.85)
    assert multiplier == 0.59 and "n=4" in (note or ""), (multiplier, note)
    assert calibration.sizing_multiplier_for(0.85, min_observations=5) == (1.0, None)
    assert calibration.sizing_multiplier_for(0.95) == (1.0, None)  # empty band
    assert calibration.sizing_multiplier_for(None) == (1.0, None)
    print("calibration sizing OK:", multiplier, "|", note)

    # Analyst scorecard: a mature decision joins its stored run results and
    # every directional analyst call is graded against realized SPY alpha.
    await run_history.init()
    today = date.today().isoformat()
    with sqlite3.connect(os.environ["DB_PATH"]) as connection:
        connection.execute(
            """
            INSERT INTO decisions (
                run_id, ticker, date, decision, confidence, price_at_decision,
                realized_return_pct, spy_return_pct, alpha_vs_spy_pct,
                mature, window_days, outlook, depth, model, policy_version
            ) VALUES (
                'run_sc', 'SC', ?, 'BUY', 0.45, 100, 3.0, 1.0, 2.0,
                1, 21, 'short_term', 'medium', 'model-m', ?
            )
            """,
            (today, calibration.DECISION_POLICY_VERSION),
        )
        connection.execute(
            """
            INSERT INTO analysis_runs (
                run_id, tickers_json, status, mock_mode, started_at,
                results_json, updated_at
            ) VALUES ('run_sc', '["SC"]', 'completed', 1, 0, ?, 0)
            """,
            (
                json.dumps(
                    {
                        "SC": {
                            "technical": {"signal": "bullish"},   # right: alpha +2
                            "news": {"signal": "bearish"},        # wrong: alpha -2
                            "sentiment": {"signal": "neutral"},   # no-call
                        }
                    }
                ),
            ),
        )
    scorecard = calibration.analyst_scorecard(min_observations=1)
    assert scorecard["window_days"] == calibration.ROLLING_WINDOW_DAYS
    assert scorecard["analysts"]["technical"]["n"] == 1
    assert scorecard["analysts"]["technical"]["hit_rate_pct"] == 100.0
    assert scorecard["analysts"]["news"]["hit_rate_pct"] == 0.0
    assert "sentiment" not in scorecard["analysts"], "neutral is a no-call"
    text = calibration.scorecard_text(scorecard)
    assert "technical: 100%" in text and "news: 0%" in text, text
    assert "none yet" in calibration.scorecard_text(
        {"analysts": {}, "min_observations": 30, "window_days": 90}
    )
    # a staler copy of the same join at the check's gate reports unavailable
    gated_scorecard = calibration.analyst_scorecard(min_observations=4)
    assert not gated_scorecard["analysts"]["technical"]["available"]
    print("analyst scorecard OK:", text)

    # Endpoint: same scope as the report, 422 on a malformed decision.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get(
            "/api/calibration",
            params={"decision": "BUY", "confidence": 0.72,
                    "outlook": "short_term", "depth": "medium"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["available"] and payload["n_mature"] == 6
        assert math.isclose(payload["directional_hit_rate"], 4 / 6)
        assert payload["confidence_bucket"] == "strong" and payload["models_pooled"] == 1

        gated = await client.get(
            "/api/calibration",
            params={"decision": "HOLD", "confidence": 0.4,
                    "outlook": "long_term", "depth": "fast"},
        )
        assert gated.status_code == 200 and not gated.json()["available"]
        assert gated.json()["n_mature"] == 0

        bad = await client.get(
            "/api/calibration", params={"decision": "YOLO", "confidence": 0.5}
        )
        assert bad.status_code == 422


asyncio.run(checks())
print("CALIBRATION CHECKS PASSED")
