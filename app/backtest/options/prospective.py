"""Manually coordinate one frozen five-session options cohort per chosen session."""

import argparse
import asyncio
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event

from pydantic import AwareDatetime

from app.backtest.options.data import MAX_DATA_BYTES, ROOT
from app.backtest.options.domain import (
    CHICAGO, EXECUTION_POLICY_VERSION, PRESETS, TICKERS, Dataset, Execution, Record, SignalArchive, calendar, schedule,
)
from app.backtest.options.identity import engine_identity
from app.backtest.options.jobs import digest, encoded
from app.backtest.options.replay import EXIT_WINDOW
from app.backtest.options.webull import WebullError, capture_lock, utc_now

RULES = PRESETS["swing_5_sessions"]
LABEL = "Prospective Luna/Fast vs momentum · polled snapshots"
RUN_ROOT = ROOT / "local-ops" / "backtests" / "options"
RESEARCH_PREFIX = "prospective-"


class Cohort(Record):
    run_id: str
    session: date
    created_at: AwareDatetime
    input_cutoff: AwareDatetime
    freeze_deadline: AwareDatetime
    entry_at: AwareDatetime
    exit_at: AwareDatetime
    comparison_at: AwareDatetime
    engine: dict
    execution: Execution
    policy_version: str


class FreezeDeadlinePassed(ValueError):
    pass


def _read(path: Path) -> dict | None:
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        data = stream.read(MAX_DATA_BYTES + 1)
    if len(data) > MAX_DATA_BYTES:
        raise ValueError("cohort artifact exceeds the local package limit")
    return json.loads(data)


def _seal(path: Path, item, *, deadline: datetime | None = None, now=utc_now) -> None:
    data = json.dumps(encoded(item), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if len(data) > MAX_DATA_BYTES:
        raise ValueError("cohort artifact exceeds the local package limit")
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        if deadline is not None and now() >= deadline:
            raise FreezeDeadlinePassed("decision-freeze deadline reached before durable publication")
        # Publish only complete bytes and refuse to overwrite frozen evidence.
        path.hardlink_to(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def _stage_failure(reason: str) -> dict:
    return {"status": "failed", "reason": reason,
            "records": [{"ticker": ticker, "status": "failed", "reason": reason} for ticker in TICKERS]}


class Prospective:
    def __init__(self, options_root: Path, *, now=utc_now):
        self.root = options_root / "prospective" / "cohorts"
        self.now = now

    def _path(self, run_id: str) -> Path:
        if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
            raise ValueError("invalid prospective cohort ID")
        return self.root / run_id

    def _cohort(self, path: Path) -> Cohort:
        raw = _read(path / "plan.json")
        if raw is None:
            raise ValueError("prospective cohort not found")
        return Cohort.model_validate(raw)

    def _check_engine(self, plan: Cohort) -> None:
        current = engine_identity(ROOT)
        key = "build_sha256" if current["kind"] == "bundled_executable" else "source_sha256"
        if not current.get(key) or current[key] != plan.engine.get(key) or plan.policy_version != EXECUTION_POLICY_VERSION:
            raise ValueError("the frozen options engine or execution policy differs from this installation")

    def plan(self, session: date) -> dict:
        cal = calendar()
        if not cal.is_session(session.isoformat()):
            raise ValueError("the declared entry date is not an exchange session")
        prior = cal.previous_session(session.isoformat())
        cutoff = cal.session_close(prior).to_pydatetime().astimezone(CHICAGO)
        deadline = datetime.combine(session, time(8, 35), CHICAGO)
        entry_at, exit_at = schedule(session, deadline, RULES)
        if entry_at.date() != session:
            raise ValueError("the declared session cannot use the frozen scheduled entry")
        run_id = digest({"study": "prospective-luna-fast-v1", "session": session})[:32]
        path = self._path(run_id)
        path.mkdir(parents=True, exist_ok=True)
        with capture_lock(path):
            if not (path / "plan.json").exists():
                if self.now() >= deadline:
                    raise ValueError("the declared session has already reached its decision-freeze deadline")
                _seal(path / "plan.json", Cohort(
                    run_id=run_id, session=session, created_at=self.now(), input_cutoff=cutoff,
                    freeze_deadline=deadline, entry_at=entry_at, exit_at=exit_at, comparison_at=exit_at + EXIT_WINDOW,
                    engine=engine_identity(ROOT), execution=Execution(), policy_version=EXECUTION_POLICY_VERSION,
                ))
        return self.details(run_id)

    @staticmethod
    def _archive(path: Path, plan: Cohort) -> SignalArchive:
        raw = _read(path / "signals.json")
        if not raw or raw.get("status") != "signals_frozen" or not raw.get("archive"):
            raise ValueError("this cohort has no complete frozen signal archive")
        archive = SignalArchive.model_validate(raw["archive"])
        if (archive.source != "imported_timestamped" or archive.candidate != "Luna/Fast" or archive.model != "gpt-6-luna"
            or {s.ticker for s in archive.signals} != set(TICKERS)
            or any(s.day != plan.session or s.input_cutoff != plan.input_cutoff or s.fast == "FAILED"
                   or s.available_at > plan.freeze_deadline for s in archive.signals)):
            raise ValueError("signal archive disagrees with the cohort's frozen cutoff or decision deadline")
        return archive

    async def freeze(self, run_id: str, *, prepare=None, generate=None) -> dict:
        path = self._path(run_id)
        plan = self._cohort(path)
        with capture_lock(path):
            output = path / "signals.json"
            if output.is_file():
                return self.details(run_id)
            self._check_engine(plan)
            attempt = path / "signals_attempt.json"
            if attempt.is_file():
                raise ValueError("the original signal attempt was interrupted; it will not be repeated")
            if self.now() < plan.input_cutoff:
                raise ValueError("the prior session's daily bars are still pending")
            if self.now() >= plan.freeze_deadline:
                _seal(output, {**_stage_failure("decision_freeze_deadline_missed"), "status": "missed"})
                return self.details(run_id)
            if prepare is None or generate is None:
                from app.backtest.options.prospective_signals import freeze_signals, prepare_inputs

                prepare = prepare or prepare_inputs
                generate = generate or freeze_signals
            prepared = _read(path / "inputs.json")
            if prepared is None:
                try:
                    prepared = encoded(await prepare(entry_at=plan.entry_at, deadline=plan.freeze_deadline))
                except Exception:  # noqa: BLE001 - preparation has not begun inference
                    _seal(output, _stage_failure("daily_input_preparation_failed"))
                    return self.details(run_id)
                _seal(path / "inputs.json", prepared)
            if self.now() >= plan.freeze_deadline:
                _seal(output, {**_stage_failure("decision_freeze_deadline_missed"), "status": "missed"})
                return self.details(run_id)
            _seal(attempt, {"started_at": self.now(), "input_sha256": digest(prepared),
                            "spending_run_id": RESEARCH_PREFIX + run_id})
            try:
                frozen = encoded(await generate(prepared, cohort_id=run_id, entry_at=plan.entry_at, deadline=plan.freeze_deadline))
            except Exception:  # noqa: BLE001 - preserve exactly-once state without printing provider credentials
                frozen = _stage_failure("signal_generation_failed")
                frozen["costs"] = {"status": "unresolved", "run_id": RESEARCH_PREFIX + run_id}
            try:
                deadline = plan.freeze_deadline if frozen.get("status") == "signals_frozen" else None
                _seal(output, frozen, deadline=deadline, now=self.now)
            except FreezeDeadlinePassed:
                frozen.update(status="missed", reason="decision_freeze_deadline_missed", archive=None)
                _seal(output, frozen)
        return self.details(run_id)

    async def entry(self, run_id: str, reference, *, capture=None) -> dict:
        from app.backtest.options.prospective_capture import capture_entry

        path = self._path(run_id)
        plan = self._cohort(path)
        with capture_lock(path):
            output = path / "entry_evidence.json"
            if output.is_file() or (path / "entry_failure.json").is_file():
                return self.details(run_id)
            self._check_engine(plan)
            self._archive(path, plan)
            attempt = path / "entry_attempt.json"
            if attempt.is_file():
                raise ValueError("the original entry capture was interrupted; entry cannot be recaptured")
            if self.now() < plan.entry_at - timedelta(minutes=2):
                raise ValueError("the entry capture window opens at 08:38 Chicago")
            if self.now() >= plan.entry_at - timedelta(seconds=2):
                _seal(path / "entry_failure.json", {"status": "missed", "reason": "entry_capture_window_missed"})
                return self.details(run_id)
            _seal(attempt, {"started_at": self.now(), "reference_sha256": digest(reference)})
            try:
                _seal(output, await (capture or capture_entry)(plan.entry_at, plan.exit_at, reference))
            except (WebullError, OSError, ValueError) as exc:
                _seal(path / "entry_failure.json", {"status": "failed", "reason": str(exc)})
        return self.details(run_id)

    def seal_entry(self, run_id: str, reference) -> dict:
        from app.backtest.options.prospective_capture import AdjustmentCoveragePending, EntryEvidence, entry_dataset
        from app.backtest.options.replay import seal_entry

        path = self._path(run_id)
        plan = self._cohort(path)
        with capture_lock(path):
            output = path / "entry_receipt.json"
            if output.is_file() or (path / "entry_failure.json").is_file():
                return self.details(run_id)
            self._check_engine(plan)
            archive = self._archive(path, plan)
            raw = _read(path / "entry_evidence.json")
            if raw is None:
                raise ValueError("record the entry capture first")
            evidence = EntryEvidence.model_validate(raw)
            if evidence.entry_at != plan.entry_at or evidence.exit_at != plan.exit_at:
                raise ValueError("entry evidence does not match the declared calendar")
            existing = _read(path / "sealed_entry_dataset.json")
            if existing is not None:
                dataset = Dataset.model_validate(existing["dataset"])
                reference_sha256 = existing["reference_sha256"]
            else:
                try:
                    dataset = entry_dataset(evidence, plan.session, reference=reference, now=self.now)
                except AdjustmentCoveragePending:
                    raise
                except ValueError as exc:
                    _seal(path / "entry_failure.json", {"status": "incomplete", "reason": str(exc)})
                    return self.details(run_id)
                reference_sha256 = digest(reference)
                _seal(path / "sealed_entry_dataset.json", {"dataset": dataset, "reference": reference,
                                                           "reference_sha256": reference_sha256})
            try:
                cohort = (plan.session, plan.entry_at, plan.exit_at, archive.signals)
                sealed = seal_entry(dataset, cohort, RULES, plan.execution)
            except ValueError as exc:
                _seal(path / "entry_failure.json", {"status": "incomplete", "reason": str(exc)})
            else:
                _seal(output, {"evidence_sha256": digest(evidence), "signal_sha256": digest(archive),
                               "dataset_sha256": digest(dataset), "reference_sha256": reference_sha256, "entry": sealed})
        return self.details(run_id)

    @staticmethod
    def _held_contracts(sealed: dict) -> list[str]:
        return sorted({trade["contract_id"] for scenario in sealed["entry"]["accounts"].values()
                       for arm in scenario.values() for trade in arm["trades"]})

    async def exit(self, run_id: str, reference, *, capture=None) -> dict:
        from app.backtest.options.prospective_capture import capture_exit

        path = self._path(run_id)
        plan = self._cohort(path)
        with capture_lock(path):
            output = path / "exit_evidence.json"
            if output.is_file() or (path / "exit_failure.json").is_file():
                return self.details(run_id)
            self._check_engine(plan)
            sealed = _read(path / "entry_receipt.json")
            if sealed is None:
                raise ValueError("the cohort has no sealed entry receipts")
            attempt = path / "exit_attempt.json"
            if attempt.is_file():
                raise ValueError("the original exit capture was interrupted; the receipt remains pending")
            if self.now() < plan.exit_at - timedelta(seconds=30):
                raise ValueError("the fifth-session capture window has not opened")
            if self.now() >= plan.comparison_at:
                _seal(path / "exit_failure.json", {"status": "incomplete", "reason": "scheduled_exit_capture_window_missed"})
                return self.details(run_id)
            contracts = self._held_contracts(sealed)
            _seal(attempt, {"started_at": self.now(), "contract_ids": contracts, "reference_sha256": digest(reference)})
            try:
                _seal(output, await (capture or capture_exit)(plan.exit_at, contracts, reference))
            except (WebullError, OSError, ValueError) as exc:
                _seal(path / "exit_failure.json", {"status": "incomplete", "reason": str(exc)})
        return self.details(run_id)

    def finish(self, run_id: str, reference=None) -> dict:
        from app.backtest.options.prospective_capture import EntryEvidence, ExitEvidence, MarketReference, replay_dataset
        from app.backtest.options.replay import replay

        path = self._path(run_id)
        plan = self._cohort(path)
        with capture_lock(path):
            output = path / "result.json"
            if output.is_file():
                return self.details(run_id)
            self._check_engine(plan)
            trade_receipt = _read(path / "entry_receipt.json")
            exit_raw = _read(path / "exit_evidence.json")
            if trade_receipt is None or exit_raw is None or self.now() < plan.comparison_at:
                raise ValueError("awaiting the scheduled exit and paired comparison evidence")
            archive = self._archive(path, plan)
            entry = EntryEvidence.model_validate(_read(path / "entry_evidence.json"))
            exit_evidence = ExitEvidence.model_validate(exit_raw)
            initial = Dataset.model_validate(_read(path / "sealed_entry_dataset.json")["dataset"])
            if trade_receipt["evidence_sha256"] != digest(entry) or trade_receipt["signal_sha256"] != digest(archive):
                raise ValueError("sealed entry evidence or signals no longer match their receipts")
            if trade_receipt["dataset_sha256"] != digest(initial) or sorted(exit_evidence.contract_ids) != self._held_contracts(trade_receipt):
                raise ValueError("exit evidence or entry dataset does not match the sealed position set")
            saved_reference = _read(path / "final_reference.json")
            if saved_reference is not None:
                if reference is not None and digest(reference) != digest(saved_reference):
                    raise ValueError("the interrupted finish already sealed a different final adjustment reference")
                reference = MarketReference.model_validate(saved_reference)
            dataset = replay_dataset(entry, exit_evidence, plan.session, reference=reference, sealed_entry=initial, now=self.now)
            cohort = (plan.session, plan.entry_at, plan.exit_at, archive.signals)
            result = replay(dataset, [cohort], RULES, plan.execution, Event(), lambda *_: None,
                            sealed_entry=trade_receipt["entry"])
            manifest = {
                "cohort": plan, "rules": RULES, "engine": plan.engine, "provider": dataset.provider,
                "resolution": dataset.resolution, "entitlement": dataset.entitlement,
                "request": {"mode": RULES.mode, "preset": RULES.preset, "source": "prospective"},
                "signal_label": "Luna/Fast and deterministic momentum · frozen before entry",
                "signal_sha256": digest(archive), "market_data_sha256": digest(dataset),
                "inputs_sha256": digest(_read(path / "inputs.json")),
                "exit_evidence_sha256": digest(exit_evidence), "final_reference_sha256": digest(reference) if reference else None,
                "signal_provenance": archive.provenance, "execution_policy_version": plan.policy_version,
                "execution": plan.execution, "account_mode": "simulated",
            }
            result.update(label=LABEL, manifest=manifest, sealed_entry=trade_receipt,
                          frozen_decisions=_read(path / "signals.json"))
            if reference and saved_reference is None:
                _seal(path / "final_reference.json", reference)
            _seal(output, result)
        return self.details(run_id)

    @staticmethod
    def _attempt_state(path: Path, active: str) -> str:
        try:
            with capture_lock(path):
                return "interrupted"
        except WebullError as exc:
            if exc.code != "capture_already_running":
                raise
            return active

    def details(self, run_id: str, *, include_result=True) -> dict | None:
        path = self._path(run_id)
        if not (path / "plan.json").is_file():
            return None
        plan = self._cohort(path)
        signal = _read(path / "signals.json")
        entry_failure = _read(path / "entry_failure.json")
        exit_failure = _read(path / "exit_failure.json")
        row = {"run_id": run_id, "started_at": plan.created_at.isoformat(), "mode": RULES.mode,
               "preset": RULES.preset, "label": LABEL, "prospective": True,
               "input_cutoff": plan.input_cutoff.isoformat(), "freeze_deadline": plan.freeze_deadline.isoformat(),
               "entry_at": plan.entry_at.isoformat(), "exit_at": plan.exit_at.isoformat(),
               "comparison_at": plan.comparison_at.isoformat()}
        result = _read(path / "result.json") if (path / "result.json").is_file() else None
        if result is not None:
            row["status"] = "incomplete" if result["incomplete"] else "complete"
            if include_result:
                row["result"] = result
        elif (failure := exit_failure or entry_failure):
            row.update(status=failure["status"], message=failure["reason"])
        elif (exit_evidence := _read(path / "exit_evidence.json")):
            if datetime.fromisoformat(exit_evidence["covered_through"]) >= plan.comparison_at:
                row.update(status="exit_recorded", message="Exit evidence recorded. Validate and finish the paired replay.")
            else:
                row.update(status="incomplete", message="The exit capture ended before the scheduled comparison mark.")
        elif (path / "exit_attempt.json").is_file():
            stage = self._attempt_state(path, "awaiting_exit")
            row.update(status=stage, message="Exit evidence remains pending.")
        elif (path / "entry_receipt.json").is_file():
            row.update(status="awaiting_exit", message="Entry sealed. Awaiting scheduled exit.")
        elif (path / "entry_evidence.json").is_file():
            row.update(status="entry_recorded", message="Entry capture recorded. Supply through-fill adjustments to seal entry.")
        elif (path / "entry_attempt.json").is_file():
            row.update(status=self._attempt_state(path, "entry_recording"), message="Entry evidence was not sealed.")
        elif signal:
            row.update(status=signal["status"], message=signal.get("reason") or signal.get("error"))
        elif (attempt := _read(path / "signals_attempt.json")):
            row.update(status=self._attempt_state(path, "signal_attempt"), message="The original signal attempt has no frozen archive.",
                       spending_run_id=attempt["spending_run_id"])
        else:
            missed = self.now() >= plan.freeze_deadline
            row.update(status="missed" if missed else "planned",
                       message="Decision-freeze deadline missed." if missed else "Waiting for frozen inputs and decisions.")
        if signal and include_result:
            row.update(signal_records=[{"ticker": s["ticker"], "status": s.get("status") or s.get("fast"),
                                        "momentum": s.get("momentum"), "reason": s.get("reason") or s.get("failure_reason")}
                                       for s in signal.get("records", [])], costs=signal.get("costs"))
        return row

    def history(self) -> list[dict]:
        return [self.details(path.parent.name, include_result=False) for path in sorted(self.root.glob("*/plan.json"))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="step", required=True)
    commands.add_parser("plan", help="declare a future exchange session").add_argument("session", type=date.fromisoformat)
    freeze = commands.add_parser("freeze", help="prepare completed daily bars and freeze Luna/Fast and momentum")
    freeze.add_argument("cohort")
    freeze.add_argument("--run-inference", action="store_true", required=True, help="authorize real Luna research against the shared cap")
    for name in ("entry", "seal-entry", "exit", "finish"):
        command = commands.add_parser(name)
        command.add_argument("cohort")
        command.add_argument("--reference", type=Path, required=name != "finish")
    status = commands.add_parser("status")
    status.add_argument("cohort")
    status.add_argument("--recover-costs", action="store_true")
    commands.add_parser("reference-schema")
    args = parser.parse_args()
    coordinator = Prospective(RUN_ROOT)
    try:
        if args.step == "plan":
            row = coordinator.plan(args.session)
        elif args.step == "status":
            row = coordinator.details(args.cohort)
            if row and args.recover_costs:
                from app.backtest.options.prospective_signals import recover_costs

                row["costs"] = asyncio.run(recover_costs(args.cohort))
        elif args.step == "freeze":
            row = asyncio.run(coordinator.freeze(args.cohort))
        else:
            from app.backtest.options.prospective_capture import MarketReference

            if args.step == "reference-schema":
                print(json.dumps(MarketReference.model_json_schema(), indent=2))
                return
            reference = MarketReference.model_validate(_read(args.reference)) if args.reference else None
            if args.step == "entry":
                row = asyncio.run(coordinator.entry(args.cohort, reference))
            elif args.step == "seal-entry":
                row = coordinator.seal_entry(args.cohort, reference)
            elif args.step == "exit":
                row = asyncio.run(coordinator.exit(args.cohort, reference))
            else:
                row = coordinator.finish(args.cohort, reference)
        if row is None:
            raise ValueError("prospective cohort not found")
        print(json.dumps({key: value for key, value in row.items() if key != "result"}, indent=2))
    except (OSError, ValueError, WebullError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
