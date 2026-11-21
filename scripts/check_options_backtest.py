"""Verify options mechanics and the integrated application with scratch storage."""

import json
import os
import socket
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
root = Path(scratch.name)
os.environ.update(DB_PATH=str(root / "options-test.db"), BTA_KEYRING_SERVICE=f"BTA-test-{root.name}",
                  OWNER_TOKEN="", LLM_API_KEY="", ALPACA_API_KEY_ID="", ALPACA_API_SECRET_KEY="",
                  ALPACA_API_KEY="", ALPACA_SECRET_KEY="", OPENAI_API_KEY="", AUTOMATION_ENABLED="0")

from app.backtest.options import api  # noqa: E402
from app.backtest.options.data import batches, coverage_for, read_archive, synthetic  # noqa: E402
from app.backtest.options.domain import CHICAGO, Dataset, Execution, PRESETS, Quote, schedule, session_bounds  # noqa: E402
from app.backtest.options.identity import engine_identity  # noqa: E402
from app.backtest.options.jobs import Jobs  # noqa: E402
from app.backtest.options.replay import Cancelled, Tape, replay  # noqa: E402
from app.main import app  # noqa: E402

archive = read_archive()
days = sorted({s.day for s in archive.signals})
assert len(days) == 23 and len(archive.signals) == 138
assert sum(s.momentum == "BUY" for s in archive.signals) == 73
assert len({s.ticker for s in archive.signals}) == 6


def check_calendar():
    def scheduled(day, preset):
        return schedule(day, datetime.combine(day, datetime.max.time(), CHICAGO), PRESETS[preset])

    entry, exit_at = scheduled(date(2025, 11, 27), "intraday_7_14_dte")
    assert entry.date().isoformat() == "2025-11-28"
    assert (exit_at.hour, exit_at.minute) == (11, 45)  # 12:00 Chicago close
    entry, exit_at = scheduled(date(2026, 1, 19), "swing_5_sessions")
    assert (entry.date().isoformat(), exit_at.date().isoformat()) == ("2026-01-20", "2026-01-26")
    entry, exit_at = scheduled(date(2026, 1, 19), "swing_10_sessions")
    assert exit_at.date().isoformat() == "2026-02-02"
    assert entry.tzinfo == CHICAGO


def check_engine():
    for rules in PRESETS.values():
        fixture, signals = synthetic(rules, archive)
        plan = batches(signals, rules, days[0], days[-1])
        out = replay(fixture, plan, rules, Execution(), Event(), lambda *_: None)
        assert not out["incomplete"] and len(out["primary"]["paired_differences"]) == 3
        for name, slip in (("primary", Decimal("0.01")), ("stress", Decimal("0.02"))):
            for arm in out[name]["accounts"].values():
                assert (arm["positions_opened"] == 9 if name == "primary" else 0 < arm["positions_opened"] <= 9)
                assert arm["fees"] == Decimal("1.30") * arm["positions_opened"]
                assert arm["peak_batch_commitment_with_fees"] <= Decimal("375")
                assert arm["ending_equity"] == Decimal("2500") + sum(t["realized_pnl"] for t in arm["trades"])
                for t in arm["trades"]:
                    assert t["contracts"] == 1 and t["multiplier"] == 100
                    assert t["entry_premium"] == t["entry_quote"].ask + slip
                    assert t["exit_premium"] == t["exit_quote"].bid - slip
                    assert t["entry_order_at"] > t["selection_at"] and t["entry_at"] >= t["entry_order_at"]
                    assert t["exit_at"] >= t["scheduled_exit"] and not t["same_day_breach"]
                assert any(r["reason"] == "shared_premium_budget_exhausted" for r in arm["skips"]) or arm["buy_signals"] == 9
        assert out["stress"]["accounts"]["fast"]["ending_equity"] < out["primary"]["accounts"]["fast"]["ending_equity"]

    rules = PRESETS["intraday_7_14_dte"]
    fixture, signals = synthetic(rules, archive)
    day, entry, scheduled_exit, rows = batches(signals, rules, days[0], days[-1])[0]
    plan = [(day, entry, scheduled_exit, rows)]
    observed_trade = replay(fixture, plan, rules, Execution(), Event(), lambda *_: None)["primary"]["accounts"]["fast"]["trades"][0]
    ticker = observed_trade["ticker"]
    expected = Tape(fixture).pick(ticker, entry, scheduled_exit, rules)[0]
    contract = expected.contract
    short = scheduled_exit - timedelta(minutes=1)
    replacements = (
        {"right": "put"}, {"deliverable": "adjusted"}, {"multiplier": 10},
        {"expiration": entry.date() + timedelta(days=6)}, {"expiration": entry.date() + timedelta(days=15)},
        {"listed_at": entry + timedelta(seconds=1)}, {"last_trade_at": short}, {"strike": Decimal(103)},
    )
    for update in replacements:
        changed = fixture.model_copy(update={"contracts": [contract.model_copy(update=update)]})
        assert Tape(changed).pick(ticker, entry, scheduled_exit, rules)[0] is None
    for days_to_expiry in (7, 14):
        changed = fixture.model_copy(update={"contracts": [contract.model_copy(update={"expiration": entry.date()+timedelta(days=days_to_expiry)})]})
        assert Tape(changed).pick(ticker, entry, scheduled_exit, rules)[0].dte == days_to_expiry
    late_entry = expected.quote.model_copy(update={"available_at": entry + timedelta(seconds=1)})
    assert Tape(fixture.model_copy(update={"quotes": [late_entry]})).pick(ticker, entry, scheduled_exit, rules)[0] is None

    target_rows = [q for q in fixture.quotes if q.contract_id == contract.id]
    no_exit = fixture.model_copy(update={"quotes": [q for q in fixture.quotes if q != target_rows[-1]],
                                         "covered_through": session_bounds(scheduled_exit)[1]})
    assert Tape(no_exit).pick(ticker, entry, scheduled_exit, rules)[0] == expected  # future availability cannot influence selection
    outcome = replay(no_exit, plan, rules, Execution(), Event(), lambda *_: None)
    arm = outcome["primary"]["accounts"]["fast"]
    assert outcome["incomplete"] and arm["ending_equity"] is None and arm["unresolved_positions"] == 1
    assert arm["intraday_breaches"] == 1 and arm["open_positions"]["cost_basis"] > 0
    broken_full = fixture.model_copy(update={"quotes": no_exit.quotes})
    full_plan = batches(signals, rules, days[0], days[-1])
    arm = replay(broken_full, full_plan, rules, Execution(), Event(), lambda *_: None)["primary"]["accounts"]["fast"]
    assert arm["unresolved_positions"] == 1 and arm["positions_opened"] == 3
    assert any(row["reason"] == "prior_position_still_open" for row in arm["skips"])
    tomorrow = scheduled_exit + timedelta(days=1, minutes=-300)
    assert session_bounds(tomorrow)[0] < tomorrow < session_bounds(tomorrow)[1]
    delayed = target_rows[-1].model_copy(update={"available_at": tomorrow, "observed_at": tomorrow})
    delayed_data = no_exit.model_copy(update={"quotes": no_exit.quotes + [delayed], "covered_through": tomorrow})
    arm = replay(delayed_data, plan, rules, Execution(), Event(), lambda *_: None)["primary"]["accounts"]["fast"]
    assert arm["intraday_breaches"] == 1 and not arm["unresolved_positions"]
    assert any(t.get("delayed_exit") and t["contract_id"] == contract.id for t in arm["trades"])

    q = target_rows[1]
    expensive = q.model_copy(update={"ask": Decimal("4.00")})
    changed = fixture.model_copy(update={"quotes": [expensive if row == q else row for row in fixture.quotes]})
    arm = replay(changed, plan, rules, Execution(), Event(), lambda *_: None)["primary"]["accounts"]["fast"]
    assert {"ticker": ticker, "reason": "unaffordable", "count": 1} in arm["skips"]
    missing = fixture.model_copy(update={"quotes": [row for row in fixture.quotes if row != q]})
    out = replay(missing, plan, rules, Execution(), Event(), lambda *_: None)
    assert not out["incomplete"]
    assert {"ticker": ticker, "reason": "missing_or_unexecutable_entry_quote", "count": 1} in out["primary"]["accounts"]["fast"]["skips"]

    # A bar already in progress when the order was submitted cannot fill it.
    same_bar = Quote(contract_id=contract.id, observed_at=entry+timedelta(minutes=1), available_at=entry+timedelta(minutes=1),
                     bar_start=entry, bid="1.00", ask="1.05")
    next_bar = same_bar.model_copy(update={"observed_at": entry+timedelta(minutes=2), "available_at": entry+timedelta(minutes=2),
                                           "bar_start": entry+timedelta(minutes=1)})
    minute = Dataset.model_validate({**fixture.model_dump(), "quotes": [same_bar, next_bar], "resolution": "minute"})
    assert Tape(minute).entry_quote(contract, entry, entry+timedelta(seconds=1), entry+timedelta(minutes=1)) is None
    assert Tape(minute).entry_quote(contract, entry, entry+timedelta(seconds=1), entry+timedelta(minutes=3)) == next_bar
    assert not coverage_for(None, plan)["supported"]
    assert not coverage_for(fixture.model_copy(update={"quotes": []}), plan)["supported"]
    cancel = Event()
    cancel.set()
    try:
        replay(fixture, plan, rules, Execution(), cancel, lambda *_: None)
    except Cancelled:
        pass
    else:
        raise AssertionError("expected cooperative cancellation")


def request(preset):
    return {"preset": preset, "mode": PRESETS[preset].mode, "source": "synthetic",
            "start": str(days[0]), "end": str(days[-1])}


def delayed_comparison():
    rules = PRESETS["intraday_7_14_dte"]
    dataset, signals = synthetic(rules, archive)
    day, entry, exit_at, rows = batches(signals, rules, days[0], days[0])[0]
    # Momentum closes AMD at 14:45:01 while Fast's NVDA closes at 14:50.
    late = exit_at + timedelta(minutes=5)
    quotes = [q.model_copy(update={"observed_at": late, "available_at": late})
              if q.contract_id.endswith("NVDA") and q.observed_at == exit_at + timedelta(seconds=1) else q
              for q in dataset.quotes if q.available_at <= exit_at + timedelta(seconds=1)]
    underlying = [observation for observation in dataset.underlying if observation.available_at <= late]
    dataset = Dataset.model_validate({**dataset.model_dump(), "quotes": quotes, "underlying": underlying, "covered_through": late})
    rows = [s.model_copy(update={"fast": "BUY" if s.ticker == "NVDA" else "HOLD",
                                 "momentum": "BUY" if s.ticker == "AMD" else "HOLD"}) for s in rows]
    return replay(dataset, [(day, entry, exit_at, rows)], rules, Execution(), Event(), lambda *_: None)


def check_comparison_and_ordering():
    result = delayed_comparison()
    assert result["incomplete"]
    primary = result["primary"]
    assert primary["execution_status"] == "complete" and primary["data_status"] == "incomplete"
    assert primary["comparison"] == {"status": "unavailable", "scheduled": 1, "usable": 0, "missing": 1,
                                      "missing_reasons": {"fast_scheduled_mark_unavailable": 1}}
    pair = primary["paired_differences"][0]
    assert pair["difference_pp"] is None and pair["at"].strftime("%H:%M:%S") == "14:47:00"
    assert primary["balances_as_of"].strftime("%H:%M:%S") == "14:50:00"
    assert primary["accounts"]["fast"]["ending_equity"] == Decimal("2515.70")
    assert primary["accounts"]["momentum"]["ending_equity"] == Decimal("2503.70")
    assert primary["accounts"]["fast"]["trades"][0]["delayed_exit"]

    rules = PRESETS["intraday_7_14_dte"]
    dataset, signals = synthetic(rules, archive)
    plan = batches(signals, rules, days[0], days[-1])
    with_gap = dataset.model_copy(update={"coverage": {day: coverage for day, coverage in dataset.coverage.items() if day != days[0]}})
    primary = replay(with_gap, plan, rules, Execution(), Event(), lambda *_: None)["primary"]
    assert primary["comparison"]["usable"] == 1 and primary["comparison"]["missing"] == 2
    assert primary["paired_differences"][0]["missing_reasons"] == ["unsupported_historical_date"]
    assert primary["paired_differences"][1]["difference_pp"] is None
    assert primary["paired_differences"][2]["difference_pp"] is not None

    _, entry, exit_at, _ = plan[0]
    contract = dataset.contracts[0]
    stamp = exit_at + timedelta(microseconds=1)
    base = dict(contract_id=contract.id, observed_at=stamp, available_at=stamp, bid_size=1, ask_size=1)
    early = Quote(**base, source_timestamp=f"{exit_at:%Y-%m-%dT%H:%M:%S}.000000101-05:00", bid="1.00", ask="1.05")
    late = Quote(**base, source_timestamp=f"{exit_at:%Y-%m-%dT%H:%M:%S}.000000899-05:00", bid="1.10", ask="1.15")
    for quotes in ([early, late], [late, early]):
        tape = Tape(Dataset.model_validate({**dataset.model_dump(), "quotes": quotes}))
        assert tape.at(contract.id, exit_at) is None  # rounded quotes remain unavailable at the boundary
        assert tape.at(contract.id, stamp) == late
        assert tape.exit_quote(contract, exit_at, stamp, Decimal("0.01")) == late
    ordered = [early.model_copy(update={"source_sequence": 8, "bid": Decimal("1.02")}),
               early.model_copy(update={"source_sequence": 7})]
    tape = Tape(Dataset.model_validate({**dataset.model_dump(), "quotes": ordered}))
    assert tape.at(contract.id, stamp) == ordered[0]
    try:
        Quote(**base, source_timestamp=f"{exit_at:%Y-%m-%dT%H:%M:%S}.000001899-05:00", bid="1", ask="1.05")
    except ValidationError as error:
        assert "rounded up" in str(error)
    else:
        raise AssertionError("source timestamps cannot occur after their normalized observation")
    try:
        Dataset.model_validate({**dataset.model_dump(), "quotes": [early, early.model_copy(update={"bid": Decimal("0.90")})]})
    except ValidationError as error:
        assert "ambiguous quote order" in str(error)
    else:
        raise AssertionError("conflicting records without chronological evidence must be rejected")


def check_engine_identity():
    fake_checkout = root / "engine"
    source = fake_checkout / "app" / "backtest" / "options" / "replay.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"rule = 1\r\n")
    with patch("app.backtest.options.identity._git", side_effect=["a" * 40, " M app/backtest/options/replay.py"]):
        original = engine_identity(fake_checkout)
    assert original["source_commit"] == "a" * 40 and original["dirty"]
    assert original["dirty_source_sha256"] == original["source_sha256"]
    source.write_bytes(b"rule = 2\n")
    with patch("app.backtest.options.identity._git", side_effect=["a" * 40, " M app/backtest/options/replay.py"]):
        updated = engine_identity(fake_checkout)
    assert original["source_sha256"] != updated["source_sha256"]


def check_api():
    api.jobs = Jobs(root / "api")
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert 'aria-current="page">Backtests' in client.get("/backtests").text
        assert len(client.get("/api/backtests").json()["presets"]) == 3
        ids, hashes = [], set()
        for preset in PRESETS:
            args = request(preset)
            result = client.get("/api/backtests/readiness", params={**args, "source": "historical"}).json()
            assert not result["ready"] and len(result["unsupported"]) == 23
            assert client.post("/api/backtests/runs", json={**args, "source": "historical"}).status_code == 409
            response = client.post("/api/backtests/runs", json=args)
            assert response.status_code == 202, response.text
            status = response.json()
            ids.append(status["run_id"])
            hashes.add(status["config_hash"])
            endpoint = f"/api/backtests/runs/{ids[-1]}"
            for _ in range(100):
                row = client.get(endpoint).json()
                if row["status"] != "running":
                    break
                time.sleep(.03)
            assert row["status"] == "complete", row
            assert row["result"]["manifest"]["rules"]["preset"] == preset
            engine = row["result"]["manifest"]["engine"]
            assert engine["source_commit"] and len(engine["source_sha256"]) == 64
            assert row["label"] == "Synthetic mechanics test"
            assert "attachment" in client.get(f"{endpoint}/export").headers["content-disposition"]
            assert client.post(f"{endpoint}/cancel").json() == {"cancel_requested": False}
        assert len(hashes) == 3 and len(set(ids)) == 3
        saved = client.get("/api/backtests/runs").json()
        assert set(ids) <= {row["run_id"] for row in saved}
        stock = next(row for row in saved if row["mode"] == "stock")
        assert client.get(f"/api/backtests/runs/{stock['run_id']}").json()["result"]["overall"]["decisions"]
        for row in saved[:3]:
            assert api.jobs.find(row["run_id"]).parts[-5:-2] == (row["mode"], row["preset"], row["config_hash"])

        with patch("app.backtest.options.jobs.ENGINE_IDENTITY", {**engine, "source_sha256": "0" * 64}), \
             patch("app.backtest.options.jobs.replay", return_value=delayed_comparison()):
            response = client.post("/api/backtests/runs", json=request("intraday_7_14_dte"))
            assert response.json()["config_hash"] not in hashes
            endpoint = f"/api/backtests/runs/{response.json()['run_id']}"
            for _ in range(100):
                if client.get(endpoint).json()["status"] != "running":
                    break
                time.sleep(.02)
            assert client.get(endpoint).json()["status"] == "incomplete"
            exported = client.get(f"{endpoint}/export").json()
            assert exported["manifest"]["engine"]["source_sha256"] == "0" * 64
            assert exported["primary"]["comparison"]["usable"] == 0
            assert exported["primary"]["paired_differences"][0]["difference_pp"] is None

        def wait_for_cancel(dataset, plan, rules, execution, cancellation, progress):
            assert cancellation.wait(5)
            raise Cancelled

        with patch("app.backtest.options.jobs.replay", side_effect=wait_for_cancel):
            response = client.post("/api/backtests/runs", json=request("intraday_7_14_dte"))
            endpoint = f"/api/backtests/runs/{response.json()['run_id']}"
            assert client.post(f"{endpoint}/cancel").json()["cancel_requested"]
            for _ in range(100):
                row = client.get(endpoint).json()
                if row["status"] == "cancelled":
                    break
                time.sleep(.02)
            assert row["status"] == "cancelled"
            assert client.get(f"{endpoint}/export").status_code == 409


def check_browser():
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    api.jobs = Jobs(root / "browser")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(accept_downloads=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}/backtests")
            page.wait_for_load_state("networkidle")
            assert page.get_by_role("heading", name="Backtests", exact=True).is_visible()
            for preset, rules in PRESETS.items():
                page.get_by_role("radio", name="Options intraday" if rules.holding_sessions == 1 else "Options swing").check()
                page.locator("#preset").select_option(preset)
                page.locator("#source").select_option("historical")
                expect(page.locator("#readiness")).to_contain_text("Historical options data required")
                expect(page.get_by_role("button", name="Run backtest")).to_be_disabled()
                page.locator("#source").select_option("synthetic")
                expect(page.locator("#readiness")).to_contain_text("3 of 3")
                page.get_by_role("button", name="Run backtest").click()
                expect(page.locator("#classification")).to_have_text("Synthetic mechanics test")
                expect(page.get_by_text("Portfolio comparison", exact=True)).to_be_visible()
                with page.expect_download() as download:
                    page.get_by_role("link", name="Export JSON").click()
                record = json.loads(Path(download.value.path()).read_bytes())
                assert record["manifest"]["rules"]["preset"] == preset
            page.reload()
            expect(page.locator("#classification")).to_contain_text("Synthetic mechanics test")
            expect(page.get_by_text("Portfolio comparison", exact=True)).to_be_visible()

            older_link = page.url.split("?", 1)[1]
            older_export = page.locator("#export").get_attribute("href")
            legacy_path = api.jobs.find(older_link.split("=", 1)[1]).with_name("result.json")
            legacy = json.loads(legacy_path.read_bytes())
            legacy["manifest"].pop("engine")
            for scenario in (legacy["primary"], legacy["stress"]):
                scenario.pop("comparison")
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
            release = Event()

            def pause(*args):
                assert release.wait(10)
                return delayed_comparison()

            page.get_by_role("radio", name="Options intraday").check()
            page.locator("#source").select_option("synthetic")
            expect(page.get_by_role("button", name="Run backtest")).to_be_enabled()
            with patch("app.backtest.options.jobs.replay", side_effect=pause):
                page.get_by_role("button", name="Run backtest").click()
                expect(page.get_by_role("progressbar")).to_be_visible()
                active_link = page.url.split("?", 1)[1]
                page.locator(f'#history a[href="?{older_link}"]').click()
                expect(page.locator("#export")).to_have_attribute("href", older_export)
                expect(page.get_by_text('Legacy result · the executing engine identity was not recorded.', exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Run backtest")).to_be_disabled()
                release.set()
                expect(page.get_by_role("button", name="Run backtest")).to_be_enabled()
                expect(page.get_by_role("progressbar")).to_be_hidden()
                expect(page.locator("#export")).to_have_attribute("href", older_export)
            expect(page.locator(f'#history a[href="?{active_link}"]')).to_be_visible()
            page.locator(f'#history a[href="?{active_link}"]').click()
            expect(page.locator("#classification")).to_contain_text("Incomplete")
            expect(page.get_by_text("Primary: execution complete · data incomplete · comparison unavailable (0 of 1 usable, 1 missing)", exact=True)).to_be_visible()
            page.get_by_text("Scheduled equity marks and paired return differences", exact=True).click()
            expect(page.get_by_text("fast scheduled mark unavailable", exact=True).first).to_be_visible()
            page.get_by_role("button", name="Run backtest").click()
            expect(page.locator("#classification")).to_have_text("Synthetic mechanics test")
            assert not errors, errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=8)
    print("Headless Edge verified all three dashboard presets, readiness, exports and reopening saved history")


check_calendar()
check_engine()
check_comparison_and_ordering()
check_engine_identity()
check_api()
if os.environ.get("BTA_CHECK_OPTIONS_BROWSER") == "1":
    check_browser()
print("Options backtest checks passed")
