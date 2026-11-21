"""Persistent replay jobs for the existing FastAPI server."""

import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
import time
from typing import Literal

from fastapi.encoders import jsonable_encoder
from pydantic import Field, model_validator

from app.backtest.options.data import ROOT, batches, coverage_for, load_dataset, read_archive, synthetic
from app.backtest.options.domain import (
    DAILY_INTRADAY_LABEL, EXECUTION_POLICY_VERSION, PRESETS, QUOTE_LABELS, REQUIRED_LABEL, SYNTHETIC_LABEL,
    Execution, Mode, Preset, Record, TICKERS,
)
from app.backtest.options.identity import engine_identity
from app.backtest.options.replay import Cancelled, replay

logger = logging.getLogger("backtests")
ENGINE_IDENTITY = engine_identity(ROOT)


def encoded(value):
    return jsonable_encoder(value, custom_encoder={Decimal: str})


def digest(value) -> str:
    raw = json.dumps(encoded(value), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class Request(Record):
    mode: Mode | Literal["stock"]
    preset: Preset | None = None
    source: Literal["historical", "synthetic", "mock"]
    start: date
    end: date
    depth: Literal["fast", "pro", "max"] = "fast"
    execution: Execution = Field(default_factory=Execution)

    @model_validator(mode="after")
    def check_request(self):
        if self.end < self.start or self.start.year < 1990 or self.end.year > 2049:
            raise ValueError("select an ordered date range between 1990 and 2049")
        if self.mode == "stock":
            if self.preset is not None or self.source != "mock" or (self.end - self.start).days > 730:
                raise ValueError("dashboard stock jobs use mock mode and span at most two years")
        elif self.source == "mock" or not self.preset or PRESETS[self.preset].mode != self.mode:
            raise ValueError("select an options preset in the chosen mode")
        return self


@lru_cache(maxsize=2)
def _cached_dataset(path: Path, preset: Preset, mtime_ns: int, size: int):
    return load_dataset(path, PRESETS[preset])


def _write(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(encoded(value), indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        for retry in range(6):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                # On Windows a simultaneous HTTP poll can briefly hold the old file open.
                if retry == 5:
                    raise
                time.sleep(0.025 * (retry + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _is_run_id(run_id: str) -> bool:
    return len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id)


class Jobs:
    def __init__(self, root: Path):
        self.root = root
        self.active: dict[str, tuple[asyncio.Task, Event]] = {}

    def metadata(self) -> dict:
        archive = read_archive()
        dates = sorted({s.day for s in archive.signals})
        return {"presets": list(PRESETS.values()), "execution": Execution(), "tickers": TICKERS,
                "signal_dates": dates, "model": archive.model, "signal_source": archive.candidate,
                "synthetic_dates": [dates[0], dates[len(dates)//2], dates[-1]]}

    def prepare(self, request: Request) -> tuple:
        rules = PRESETS[request.preset]
        archive = read_archive()
        dataset = identity = issue = None
        if request.source == "synthetic":
            dataset, archive = synthetic(rules, archive)
            identity = digest(dataset)
        else:
            path = self.root / rules.mode / rules.preset / "data" / "historical.json"
            try:
                stat = path.stat()
                dataset, identity = _cached_dataset(path, rules.preset, stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                issue = "Import point-in-time option chains, bid/ask execution quotes, underlying observations and deliverable/adjustment evidence for this preset."
            except (OSError, ValueError):
                issue = "Historical import failed schema or preset validation. Check the local import against the protocol."
        plan = batches(archive, rules, request.start, request.end)
        coverage = coverage_for(dataset, plan)
        label = SYNTHETIC_LABEL if request.source == "synthetic" else REQUIRED_LABEL
        if request.source == "historical" and coverage["supported"]:
            label = QUOTE_LABELS[dataset.resolution]
        ready = {**coverage, "ready": bool(coverage["supported"]), "label": label,
                 "issue": issue or ("No scheduled signal dates with complete coverage in this range." if not coverage["supported"] else None),
                 "provider": dataset.provider if dataset else None, "entitlement": dataset.entitlement if dataset else None}
        return rules, archive, dataset, identity, plan, ready

    def _archive_files(self) -> dict:
        candidates = sorted((ROOT / "backtests").glob("report-*.json"))
        candidates += sorted((ROOT / "backtests" / "live-sweep").glob("*/report-*.json"))
        candidates += sorted((ROOT / "local-ops" / "backtests").glob("report-install-*.json"))
        return {"archive-" + hashlib.sha256(str(p.relative_to(ROOT)).encode()).hexdigest()[:16]: p for p in candidates}

    def history(self) -> list:
        rows = [self._status(path) for path in self.root.glob("*/*/*/*/status.json")]
        existing = {row["run_id"] for row in rows}
        for receipt in (self.root / "requests").glob("*.json"):
            if receipt.stem not in existing and (status := self._request_status(receipt.stem)):
                rows.append(status)
        rows = sorted(rows, key=lambda r: r.get("started_at", ""), reverse=True)
        rows.extend({"run_id": run_id, "mode": "stock", "status": "complete", "label": path.name,
                     "preset": (path.parent.name if path.parent.parent.name == "live-sweep" else None)}
                    for run_id, path in self._archive_files().items())
        return rows

    def _status(self, path: Path) -> dict:
        for retry in range(5):
            try:
                raw = path.read_bytes()
                break
            except PermissionError:
                # Windows readers can also briefly race the atomic status replacement.
                time.sleep(0.025 * (retry + 1))
        else:
            raw = path.read_bytes()
        return self._interrupt_if_inactive(json.loads(raw))

    def _interrupt_if_inactive(self, status: dict) -> dict:
        if status["status"] == "running" and status["run_id"] not in self.active:
            status.update(status="interrupted", message="Server stopped before this run finished.")
        return status

    def find(self, run_id: str) -> Path | None:
        if not _is_run_id(run_id):
            return None
        return next(self.root.glob(f"*/*/*/{run_id}/status.json"), None)

    def _request_status(self, run_id: str, fingerprint: str | None = None) -> dict | None:
        if not _is_run_id(run_id):
            return None
        try:
            receipt = json.loads((self.root / "requests" / f"{run_id}.json").read_bytes())
        except FileNotFoundError:
            return None
        if fingerprint is not None and receipt["request_sha256"] != fingerprint:
            raise ValueError("This Idempotency-Key was already used for a different options backtest request.")
        status = receipt["status"]
        path = self.root / status["mode"] / status["preset"] / status["config_hash"] / run_id / "status.json"
        try:
            return self._status(path)
        except FileNotFoundError:
            # A stopped server may have claimed the request before creating the run files.
            return self._interrupt_if_inactive(status)

    def _claim_request(self, run_id: str, request_fingerprint: str, status: dict) -> bool:
        path = self.root / "requests" / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False)
        staged = Path(temporary.name)
        try:
            with temporary:
                json.dump(encoded({"request_sha256": request_fingerprint, "status": status}), temporary)
            # Publish the complete receipt exclusively, before any replay can start.
            path.hardlink_to(staged)
        except FileExistsError:
            return False
        finally:
            staged.unlink(missing_ok=True)
        return True

    def details(self, run_id: str) -> dict | None:
        path = self.find(run_id)
        if path:
            status = self._status(path)
            output = path.with_name("result.json")
            if status["status"] in ("complete", "incomplete") and output.is_file():
                status["result"] = json.loads(output.read_bytes())
            return status
        archived = self._archive_files().get(run_id)
        if archived:
            return {"run_id": run_id, "mode": "stock", "status": "complete", "label": archived.name,
                    "result": json.loads(archived.read_bytes())}
        return self._request_status(run_id)

    async def start(self, request: Request, idempotency_key: uuid.UUID | None = None) -> dict:
        fingerprint = None
        if request.mode == "stock":
            run_id = uuid.uuid4().hex
        else:
            if idempotency_key is None:
                raise ValueError("Options backtests require a UUID Idempotency-Key.")
            run_id = hashlib.sha256(b"options-backtest-request\0" + idempotency_key.bytes).hexdigest()[:32]
            fingerprint = digest(request)
            if existing := self._request_status(run_id, fingerprint):
                return existing
        if self.active:
            raise ValueError("Another backtest is running. Finish or cancel it first.")
        prepared = None
        if request.mode != "stock":
            prepared = await asyncio.to_thread(self.prepare, request)
            if fingerprint and (existing := self._request_status(run_id, fingerprint)):
                return existing
            if not prepared[-1]["ready"]:
                raise ValueError(prepared[-1]["issue"] or REQUIRED_LABEL)
        elif getattr(sys, "frozen", False):
            raise ValueError("Starting stock replays requires the source installation and its stock backtest CLI.")
        # A request may finish preparing while another request creates its task.
        if self.active:
            raise ValueError("Another backtest is running. Finish or cancel it first.")
        manifest = {"schema_version": 1, "request": request, "run_id": run_id,
                    "started_at": datetime.now(timezone.utc)}
        if prepared:
            rules, archive, dataset, identity, _, ready = prepared
            manifest.update(account_mode="simulated", execution_policy_version=EXECUTION_POLICY_VERSION,
                            rules=rules, engine=ENGINE_IDENTITY, market_data_sha256=identity, provider=dataset.provider,
                            entitlement=dataset.entitlement, resolution=dataset.resolution, coverage=ready,
                            signal_source=archive.source, candidate=archive.candidate, model=archive.model,
                            signal_sha256=digest(archive), provenance=archive.provenance,
                            signal_label=DAILY_INTRADAY_LABEL if request.mode == "options_intraday_v1" and archive.source == "archived_daily" else None,
                            fixed_rules={"strike_distance_pct": 2, "relative_spread_pct": 10, "absolute_spread": "0.20",
                                         "selection_time_chicago": "08:40", "exit_minutes_before_close": 15,
                                         "tick_freshness_seconds": 2, "tick_size_required": True,
                                         "order_delay_seconds": 1, "tick_fill_delay_seconds": 3,
                                         "minute_fill_delay_minutes": 3, "scheduled_exit_window_minutes": 2,
                                         "calendar": "XNYS", "calendar_version": _calendar_version(),
                                         "contracts_per_ticker": 1, "after_missing_exit": "first executable same-contract quote before last trade/adjustment"})
        config_hash = digest({k: v for k, v in manifest.items() if k not in ("run_id", "started_at", "coverage")})
        manifest["config_hash"] = config_hash
        destination = self.root / request.mode / (request.preset or "mock") / config_hash / run_id
        label = prepared[-1]["label"] if prepared else "Stock mock backtest"
        status = {"run_id": run_id, "status": "running", "label": label, "mode": request.mode,
                  "preset": request.preset, "started_at": manifest["started_at"], "progress": 0, "total": 0,
                  "config_hash": config_hash}
        if fingerprint and not self._claim_request(run_id, fingerprint, status):
            existing = self._request_status(run_id, fingerprint)
            assert existing is not None  # A successful receipt is never removed by the replay lifecycle.
            return existing
        destination.mkdir(parents=True, exist_ok=False)
        _write(destination / "manifest.json", manifest)
        _write(destination / "status.json", status)
        cancel = Event()
        task = asyncio.create_task(self._run(request, prepared, destination, status, manifest, cancel), name=f"backtest:{run_id}")
        self.active[run_id] = task, cancel
        return status

    async def _run(self, request, prepared, destination, status, manifest, cancel):
        def progress(done, total):
            status.update(progress=done, total=total)
            _write(destination / "status.json", status)

        try:
            if prepared:
                rules, _, dataset, _, plan, _ = prepared
                result = await asyncio.to_thread(replay, dataset, plan, rules, request.execution, cancel, progress)
                result.update(manifest=manifest, label=status["label"])
            else:
                result = await self._stock(request, destination, cancel)
            if cancel.is_set():
                raise Cancelled
            _write(destination / "result.json", result)
            status["status"] = "incomplete" if result.get("incomplete") else "complete"
        except (Cancelled, asyncio.CancelledError):
            status["status"] = "cancelled"
        except Exception:
            logger.exception("Backtest failed: %s", status["run_id"])
            status.update(status="failed", message="Backtest failed. Check the local server log.")
        finally:
            try:
                _write(destination / "status.json", status)
            finally:
                self.active.pop(status["run_id"], None)

    async def _stock(self, request: Request, destination: Path, cancel: Event):
        cache = self.root / "stock" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHON_DOTENV_DISABLED": "1", "LLM_API_KEY": "", "FINNHUB_API_KEY": "",
               "DB_PATH": str(destination / "scratch.db"), "BACKTEST_CACHE": str(cache / "cache.db")}
        with (destination / "stock.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.backtest", "--tickers", ",".join(TICKERS),
                "--start", request.start.isoformat(), "--end", request.end.isoformat(),
                "--depth", request.depth, "--out", str(destination), stdout=log, stderr=log, cwd=ROOT, env=env,
            )
            try:
                while process.returncode is None:
                    if cancel.is_set():
                        raise Cancelled
                    await asyncio.sleep(0.2)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        if process.returncode:
            raise RuntimeError("stock CLI failed")
        return json.loads((destination / "report-mock.json").read_bytes())

    def cancel(self, run_id: str) -> bool:
        entry = self.active.get(run_id)
        if entry:
            entry[1].set()
        return bool(entry)


def _calendar_version() -> str:
    from exchange_calendars import __version__

    return __version__
