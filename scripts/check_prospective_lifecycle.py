"""Check the installed prospective coordinator against an offline cash cohort."""

import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.backtest.options.domain import CHICAGO, TICKERS, Signal, SignalArchive, Underlying
from app.backtest.options.jobs import Jobs, digest
from app.backtest.options.prospective import Prospective
from app.backtest.options.prospective_capture import EntryEvidence, ExitEvidence, MarketReference
from app.backtest.options.replay import TICK_ENTRY_DELAY


DAY = date(2026, 11, 24)
EXAMPLE_SOURCE = {"kind": "source", "source_commit": "offline-fixture", "source_sha256": "fixture", "dirty": False}


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


async def check(root: Path):
    prior_close = datetime.combine(DAY - timedelta(days=1), time(15), CHICAGO)
    clock = Clock(prior_close - timedelta(minutes=1))
    study = Prospective(root, now=clock)
    service = Jobs(root)
    calls = []
    plan = study.plan(DAY)
    run_id = plan["run_id"]
    cohort_path = root / "prospective" / "cohorts" / run_id
    assert plan["status"] == "planned"
    assert study.plan(DAY)["run_id"] == run_id
    assert service.details(run_id)["prospective"]
    assert datetime.fromisoformat(plan["input_cutoff"]) == prior_close
    entry_at, exit_at, comparison_at = (datetime.fromisoformat(plan[key])
                                        for key in ("entry_at", "exit_at", "comparison_at"))
    assert entry_at.isoformat() == "2026-11-24T08:40:00-06:00"
    assert exit_at.isoformat() == "2026-12-01T14:45:00-06:00"
    assert comparison_at.isoformat() == "2026-12-01T14:47:00-06:00"

    inputs = {"source": "fixture", "symbols": list(TICKERS), "fixture_close": "100.00"}

    async def prepare(*, entry_at, deadline):
        assert entry_at == datetime.fromisoformat(plan["entry_at"])
        assert clock() < deadline
        assert not (cohort_path / "signals_attempt.json").exists()
        calls.append("prepare")
        return inputs

    async def generate(snapshot, *, cohort_id, entry_at, deadline):
        assert cohort_id == run_id
        assert clock() < deadline < entry_at
        assert json.loads((cohort_path / "inputs.json").read_bytes()) == snapshot == inputs
        attempt = json.loads((cohort_path / "signals_attempt.json").read_bytes())
        assert attempt["input_sha256"] == digest(snapshot)
        assert attempt["spending_run_id"] == f"prospective-{run_id}"
        calls.append("generate")
        archive = SignalArchive(
            source="imported_timestamped", candidate="Luna/Fast", model="gpt-6-luna",
            provenance={"prompts": {"manager": "offline fixture"}, "prompt_sha256": {"manager": "fixture"},
                        "settings": {"depth": "fast", "outlook": "short_term"}, "input_source": "offline fixture"},
            signals=[Signal(day=DAY, ticker=ticker, fast="HOLD", momentum="HOLD", input_cutoff=prior_close,
                            generated_at=clock(), available_at=clock(), research_cost="0.01") for ticker in TICKERS],
        )
        return {"status": "signals_frozen", "archive": archive, "records": archive.signals,
                "costs": {"status": "estimated", "dollars": "0.06"}}

    try:
        await study.freeze(run_id, prepare=prepare, generate=generate)
    except ValueError as error:
        assert "daily bars are still pending" in str(error)
    else:
        raise AssertionError("the prior completed input cutoff has not arrived")
    assert not calls

    clock.at = entry_at - timedelta(minutes=10)
    frozen = await study.freeze(run_id, prepare=prepare, generate=generate)
    assert frozen["status"] == "signals_frozen" and len(frozen["signal_records"]) == len(TICKERS)
    study = Prospective(root, now=clock)  # Recover the durable cohort as a new process would.
    assert await study.freeze(run_id, prepare=prepare, generate=generate) == frozen
    assert calls == ["prepare", "generate"]

    clock.at = entry_at - timedelta(minutes=1)
    spots = [Underlying(ticker=ticker, price=100, observed_at=clock(), available_at=clock()) for ticker in TICKERS]
    reference = MarketReference(underlying=spots, underlying_source="offline fixture",
                                adjustments_checked_through=clock(), adjustments_source="offline fixture")

    async def capture_entry(target, scheduled_exit, supplied):
        assert target == entry_at and scheduled_exit == exit_at and supplied == reference
        calls.append("entry")
        started = clock()
        clock.at = entry_at + TICK_ENTRY_DELAY
        return EntryEvidence(entry_at=target, exit_at=scheduled_exit, started_at=started, covered_through=clock(),
                             reference=supplied, spots=spots, contracts=[], inventory=[], discovery_attempts=[],
                             batches=[], gaps={})

    recorded = await study.entry(run_id, reference, capture=capture_entry)
    assert recorded["status"] == "entry_recorded"
    assert await study.entry(run_id, reference, capture=capture_entry) == recorded
    assert calls.count("entry") == 1
    through_fill = reference.model_copy(update={"adjustments_checked_through": clock()})
    sealed = study.seal_entry(run_id, through_fill)
    assert sealed["status"] == "awaiting_exit"
    assert study.seal_entry(run_id, through_fill) == sealed
    entry_receipt = json.loads((cohort_path / "entry_receipt.json").read_bytes())
    assert not entry_receipt["entry"]["accounts"]["primary"]["fast"]["trades"]
    assert Decimal(entry_receipt["entry"]["accounts"]["primary"]["fast"]["cash"]) == Decimal("2499.94")

    async def capture_exit(target, held_contracts, supplied):
        assert target == exit_at and held_contracts == []
        calls.append("exit")
        started = clock()
        clock.at = comparison_at
        return ExitEvidence(exit_at=target, started_at=started, covered_through=clock(),
                            contract_ids=held_contracts, reference=supplied, batches=[], gaps=[])

    try:
        await study.exit(run_id, through_fill, capture=capture_exit)
    except ValueError as error:
        assert "capture window has not opened" in str(error)
    else:
        raise AssertionError("future exit cannot create evidence")
    try:
        study.finish(run_id)
    except ValueError as error:
        assert "awaiting the scheduled exit" in str(error)
    else:
        raise AssertionError("final replay must wait for the scheduled exit and the paired mark")
    assert "exit" not in calls and not (cohort_path / "result.json").exists()

    clock.at = exit_at - timedelta(seconds=5)
    exited = await study.exit(run_id, through_fill, capture=capture_exit)
    assert exited["status"] == "exit_recorded"
    assert await study.exit(run_id, through_fill, capture=capture_exit) == exited
    finished = study.finish(run_id)
    assert finished["status"] == "complete" and finished["result"]["sealed_entry_reproduced"]
    assert finished["result"]["sealed_entry"] == entry_receipt
    assert finished["result"]["manifest"]["request"]["source"] == "prospective"
    assert finished["result"]["primary"]["comparison"]["usable"] == 1
    assert study.finish(run_id) == finished
    assert service.details(run_id) == finished
    history_row = next(row for row in service.history() if row["run_id"] == run_id)
    assert history_row["status"] == "complete" and history_row["prospective"] and "result" not in history_row
    assert calls == ["prepare", "generate", "entry", "exit"]


if __name__ == "__main__":
    with TemporaryDirectory() as temporary:
        with patch("app.backtest.options.prospective.engine_identity", return_value=EXAMPLE_SOURCE):
            with patch.object(Jobs, "_archive_files", return_value={}):
                asyncio.run(check(Path(temporary)))
    print("Prospective coordinator, immutable retry and saved results verified (offline fixture)")
