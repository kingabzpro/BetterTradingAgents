"""Offline checks for the watchlist and decision-change workflow (ROADMAP P1.4).

Run: PYTHONPATH=. uv run python scripts/check_watchlist.py
No network: prices are stubbed and run history is written directly to SQLite.
"""

import asyncio
import os
from pathlib import Path
import tempfile

# Isolated DB before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "watchlist_test.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""

import httpx  # noqa: E402

from app import broker, run_history, watchlist  # noqa: E402
from app.main import app  # noqa: E402
from app.models import RunStatus, StockAnalysis  # noqa: E402


async def fake_price(ticker: str):
    return {"AAPL": 200.0, "NVDA": 120.0, "AMD": 100.0}.get(ticker, 50.0)


watchlist.get_current_price = fake_price

OWNER = "device_watchlist_test"


def _analysis(ticker: str, decision: str, confidence: float, price: float) -> StockAnalysis:
    return StockAnalysis(
        ticker=ticker,
        company_name=f"{ticker} Inc.",
        price=price,
        decision=decision,  # type: ignore[arg-type]
        confidence=confidence,
        as_of="2026-09-20T12:00:00+00:00",
        risk_flags=[] if decision != "SELL" else ["position size capped"],
    )


async def checks() -> None:
    await broker.init()
    await run_history.init()
    await watchlist.init()

    # ---- empty listing and invalid ticker ------------------------------------
    assert await watchlist.list_watchlist(OWNER) == []
    try:
        await watchlist.add_item(OWNER, "NOT A TICKER!")
        raise AssertionError("invalid ticker must be rejected")
    except ValueError as exc:
        assert "invalid ticker" in str(exc)

    # ---- manual add without a baseline run: not_reanalyzed -------------------
    manual, already = await watchlist.add_item(
        OWNER, "amd", note="momentum name", outlook="long_term", depth="expert"
    )
    assert not already and manual.ticker == "AMD"
    assert manual.change_status == "not_reanalyzed"
    assert manual.last_call is None and manual.current_call is None
    assert manual.outlook == "long_term" and manual.depth == "expert"
    print("manual add OK:", manual.ticker, manual.change_status)

    # ---- save a result as baseline, then no newer run ------------------------
    run1 = RunStatus(
        run_id="watchrun0001",
        tickers=["AAPL"],
        outlook="short_term",
        depth="medium",
        status="completed",
        started_at=1_780_000_000,
        duration_s=10.0,
        results={"AAPL": _analysis("AAPL", "BUY", 0.72, 190.0)},
    )
    await run_history.save(run1, completed_at=1_780_000_010.0, owner_id=OWNER)

    saved, already = await watchlist.add_item(
        OWNER, "AAPL", note="from result", run_id="watchrun0001"
    )
    assert not already
    assert saved.last_call is not None
    assert saved.last_call.decision == "BUY"
    assert saved.last_call.confidence == 0.72
    assert saved.change_status == "not_reanalyzed"
    assert saved.current_call is None
    assert saved.view_run_id == "watchrun0001"
    assert saved.live_price == 200.0
    assert saved.price_move_pct is not None and saved.price_move_pct > 0
    print("baseline save OK:", saved.ticker, "live move", saved.price_move_pct)

    # Re-save without run_id keeps the baseline and flags already_watched.
    again, already = await watchlist.add_item(OWNER, "aapl", note="updated note")
    assert already and again.note == "updated note"
    assert again.last_call and again.last_call.run_id == "watchrun0001"
    print("re-save keeps baseline OK")

    # ---- newer run, same decision + evidence bucket -> no_change -------------
    run2 = RunStatus(
        run_id="watchrun0002",
        tickers=["AAPL"],
        outlook="short_term",
        depth="medium",
        status="completed",
        started_at=1_780_100_000,
        duration_s=11.0,
        results={"AAPL": _analysis("AAPL", "BUY", 0.70, 195.0)},
    )
    await run_history.save(run2, completed_at=1_780_100_011.0, owner_id=OWNER)
    items = await watchlist.list_watchlist(OWNER)
    aapl = next(i for i in items if i.ticker == "AAPL")
    assert aapl.change_status == "no_change", aapl.change_status
    assert not aapl.decision_changed and not aapl.evidence_changed
    assert aapl.current_call is not None and aapl.current_call.run_id == "watchrun0002"
    assert aapl.view_run_id == "watchrun0002"
    print("no_change OK: confidence moved within the same strong bucket")

    # ---- newer run with different decision + bucket -> changed ---------------
    run3 = RunStatus(
        run_id="watchrun0003",
        tickers=["AAPL"],
        outlook="short_term",
        depth="medium",
        status="completed",
        started_at=1_780_200_000,
        duration_s=12.0,
        results={"AAPL": _analysis("AAPL", "SELL", 0.40, 175.0)},
    )
    await run_history.save(run3, completed_at=1_780_200_012.0, owner_id=OWNER)
    items = await watchlist.list_watchlist(OWNER)
    aapl = next(i for i in items if i.ticker == "AAPL")
    assert aapl.change_status == "changed"
    assert aapl.decision_changed and aapl.evidence_changed
    assert aapl.last_call.decision == "BUY"
    assert aapl.current_call.decision == "SELL"
    assert "position size capped" in aapl.unresolved_risk_flags
    print("changed OK:", aapl.last_call.decision, "->", aapl.current_call.decision)

    # ---- failed / incomplete results are not treated as reanalysis -----------
    # AMD has no completed run with a result: still not_reanalyzed even though
    # other runs exist for the owner.
    items = await watchlist.list_watchlist(OWNER)
    amd = next(i for i in items if i.ticker == "AMD")
    assert amd.change_status == "not_reanalyzed"
    assert amd.current_call is None
    print("not reanalyzed without a result OK")

    # ---- update note / remove / clear ----------------------------------------
    updated = await watchlist.update_item(OWNER, amd.id, "swing idea", "day_trade", "fast")
    assert updated.note == "swing idea"
    assert updated.outlook == "day_trade" and updated.depth == "fast"
    removed = await watchlist.remove_item(OWNER, amd.id)
    assert removed == "AMD"
    try:
        await watchlist.remove_item(OWNER, amd.id)
        raise AssertionError("double delete must 404")
    except LookupError:
        pass

    # ---- API surface ----------------------------------------------------------
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/api/watchlist")
        assert missing.status_code == 200 and missing.json() == []

        created = await client.post(
            "/api/watchlist",
            headers={"X-Client-ID": OWNER},
            json={"ticker": "nvda", "note": "chip", "run_id": None},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["item"]["ticker"] == "NVDA"
        assert body["already_watched"] is False
        item_id = body["item"]["id"]

        listed = await client.get("/api/watchlist", headers={"X-Client-ID": OWNER})
        assert listed.status_code == 200
        tickers = {i["ticker"] for i in listed.json()}
        assert "AAPL" in tickers and "NVDA" in tickers

        bad = await client.post(
            "/api/watchlist", headers={"X-Client-ID": OWNER}, json={"ticker": "!!"}
        )
        assert bad.status_code == 400

        no_header = await client.post("/api/watchlist", json={"ticker": "MSFT"})
        assert no_header.status_code == 400

        patched = await client.patch(
            f"/api/watchlist/{item_id}",
            headers={"X-Client-ID": OWNER},
            json={"note": "ai leader"},
        )
        assert patched.status_code == 200 and patched.json()["note"] == "ai leader"

        # Owner isolation: another client cannot see or delete this row.
        foreign = await client.get(
            "/api/watchlist", headers={"X-Client-ID": "device_other_watch"}
        )
        assert foreign.json() == []
        foreign_del = await client.delete(
            f"/api/watchlist/{item_id}", headers={"X-Client-ID": "device_other_watch"}
        )
        assert foreign_del.status_code == 404

        page = await client.get("/watchlist")
        assert page.status_code == 200
        assert "Watched tickers" in page.text
        assert "Analyze selected" in page.text

        cleared = await client.delete(
            "/api/watchlist", headers={"X-Client-ID": OWNER}
        )
        assert cleared.status_code == 200 and cleared.json()["deleted"] >= 2

    # ---- persistence across restart (init is idempotent) ----------------------
    await watchlist.init()
    reloaded = await watchlist.list_watchlist(OWNER)
    assert reloaded == []
    print("API + page + clear OK")

    # ---- evidence buckets match the UI ---------------------------------------
    assert watchlist.evidence_bucket(0.70) == "strong"
    assert watchlist.evidence_bucket(0.50) == "moderate"
    assert watchlist.evidence_bucket(0.49) == "low"
    assert watchlist.evidence_bucket(None) == "unknown"
    print("evidence buckets OK")


asyncio.run(checks())
print("WATCHLIST CHECKS PASSED")
