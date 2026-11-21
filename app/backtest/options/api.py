"""Backtests use the application's existing dashboard and local-only guard."""

import asyncio
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from app import pages
from app.backtest.options.data import ROOT
from app.backtest.options.domain import Mode, Preset
from app.backtest.options.jobs import Jobs, Request, encoded
from app.config import BASE_DIR, settings

router = APIRouter()
jobs = Jobs(BASE_DIR / "local-ops" / "backtests" / "options")


@router.get("/backtests", include_in_schema=False)
def backtests_page():
    features = {key: settings.experimental_features and getattr(settings, f"feature_{key}", False)
                for key, _, _ in pages.FEATURE_LINKS}
    return pages.render_page("backtests.html", "backtests", features)


@router.get("/backtests/market-data", include_in_schema=False, response_class=PlainTextResponse)
def data_instructions():
    protocol = ROOT / "backtests" / "options" / "protocol.md"
    if protocol.is_file():
        return protocol.read_text(encoding="utf-8")
    return "See backtests/options/protocol.md in the source repository for local historical data and import instructions."


@router.get("/api/backtests")
def metadata():
    return encoded(jobs.metadata())


@router.get("/api/backtests/readiness")
def readiness(mode: Mode, preset: Preset, source: str = "historical", start: date | None = None, end: date | None = None):
    dates = jobs.metadata()["signal_dates"]
    try:
        request = Request(mode=mode, preset=preset, source=source, start=start or dates[0], end=end or dates[-1])
        return encoded(jobs.prepare(request)[-1])
    except ValueError as exc:
        raise HTTPException(422, "Select a valid preset, source and date range.") from exc


@router.get("/api/backtests/runs")
def history():
    return jobs.history()


@router.post("/api/backtests/runs", status_code=202)
async def start(request: Request, idempotency_key: Annotated[str | None, Header()] = None):
    key = None
    if request.mode != "stock":
        try:
            key = UUID(idempotency_key or "")
        except ValueError as exc:
            raise HTTPException(422, "Options backtests require a UUID Idempotency-Key.") from exc
    try:
        return encoded(await jobs.start(request, key))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/backtests/runs/{run_id}")
def details(run_id: str):
    result = jobs.details(run_id)
    if result is None:
        raise HTTPException(404, "Backtest not found")
    return result


@router.post("/api/backtests/runs/{run_id}/cancel")
async def cancel(run_id: str):
    requested = jobs.cancel(run_id)
    if not requested and await asyncio.to_thread(jobs.details, run_id) is None:
        raise HTTPException(404, "Backtest not found")
    return {"cancel_requested": requested}


@router.get("/api/backtests/runs/{run_id}/export")
def export(run_id: str):
    row = details(run_id)
    if "result" not in row:
        raise HTTPException(409, "This run has no result to export")
    return JSONResponse(row["result"], headers={"Content-Disposition": f'attachment; filename="backtest-{run_id}.json"'})
