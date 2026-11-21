"""Causal, durable signal inputs for the Luna/Fast five-session study."""

import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from math import isfinite
import re

from app.backtest.options.data import ROOT
from app.backtest.options.domain import CHICAGO, EXECUTION_POLICY_VERSION, TICKERS, Signal, SignalArchive, calendar
from app.backtest.options.identity import engine_identity

CANDIDATE = "Luna/Fast"
MODEL = "gpt-6-luna"
OUTLOOK = "short_term"
DECISION_POLICY = "prospective-luna-fast-v1"
SOURCE = "yfinance adjusted daily bars"
AGENTS = ("technical", "forecast", "manager")
PRICE_FIELDS = ("open", "high", "low", "close")
INPUT_FILES = (
    "app/agents/__init__.py", "app/agents/technical.py", "app/agents/forecast.py", "app/agents/manager.py",
    "app/backtest/grade.py", "app/config.py", "app/depth.py", "app/discovery.py", "app/outlook.py",
    "app/risk.py", "app/spending.py", "app/tools/indicators.py", "app/workflow.py",
)
ACTIVE_TICKER: ContextVar[str] = ContextVar("prospective_signal_ticker")


class SignalStudyError(ValueError):
    """A prospective study cannot use the supplied date, inputs, or backend."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def input_window(entry_at: datetime, deadline: datetime) -> tuple[datetime, date, list[date]]:
    """Return the prior completed session close and the last six months of sessions."""
    if entry_at.tzinfo is None or deadline.tzinfo is None:
        raise SignalStudyError("Entry and freeze deadline require timezone-aware timestamps.")
    entry = entry_at.astimezone(CHICAGO)
    expected_entry = datetime.combine(entry.date(), time(8, 40), CHICAGO)
    if entry_at != expected_entry or deadline != expected_entry - timedelta(minutes=5):
        raise SignalStudyError("The study requires an 08:40 Chicago entry and 08:35 freeze deadline.")
    cal = calendar()
    day = entry.date()
    if not cal.is_session(day.isoformat()):
        raise SignalStudyError("The declared entry date is not an exchange session.")
    previous = cal.previous_session(day.isoformat())
    cutoff = cal.session_close(previous).to_pydatetime().astimezone(CHICAGO)
    sessions = cal.sessions_in_range((day - timedelta(days=183)).isoformat(), previous)
    return cutoff, day, [session.date() for session in sessions]


def _runtime():
    try:
        from app import spending, workflow
    except ImportError as exc:
        if exc.name not in ("app.spending", "spending", "app") and "import name 'spending'" not in str(exc):
            raise
        raise SignalStudyError("The installed shared research spending guard is required.") from exc
    if (getattr(spending, "MODEL", None) != MODEL or not callable(getattr(spending, "guard_client", None))
        or not callable(getattr(workflow, "pinned_analysis_clients", None))):
        raise SignalStudyError("The installed Luna spending guard and pinned study workflow are required.")
    return spending, workflow


def _configuration(spending, workflow) -> tuple[dict, dict[str, str]]:
    settings = workflow.settings
    configurations = {}
    keys = {}
    for agent in AGENTS:
        configuration = settings.llm_for(settings.llm_key_for(agent))
        if (workflow._api_model_name(configuration["model"]) != MODEL
            or configuration["base_url"].rstrip("/") != spending.OFFICIAL_BASE
            or not configuration["api_key"] or not settings.llm_configured):
            raise SignalStudyError("Every Fast agent needs the configured real OpenAI Luna backend.")
        reasoning = (settings.llm_reasoning_effort_manager if agent == "manager" else "") or settings.llm_reasoning_effort
        configurations[agent] = {
            "configured_model": configuration["model"], "requested_model": MODEL,
            "base_url": spending.OFFICIAL_BASE, "temperature": settings.llm_temperature,
            "timeout_seconds": settings.llm_timeout_seconds, "max_tokens": settings.llm_max_tokens or None,
            "reasoning_effort": reasoning or None, "stream": False, "json_mode": True, "retries": 0,
        }
        keys[agent] = configuration["api_key"]
    return {
        "candidate": CANDIDATE, "depth": "fast", "outlook": OUTLOOK, "forecast": "local",
        "memory": "isolated", "portfolio": "isolated", "llm_concurrency": settings.llm_concurrency,
        "agents": configurations,
    }, keys


def _source_fingerprints() -> dict:
    return {
        path: sha256((ROOT / path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for path in INPUT_FILES
    }


def _fetch_yfinance(ticker: str, first: date, end: date) -> list[dict]:
    from app.tools.market_data import _yf

    frame = _yf().Ticker(ticker).history(start=first.isoformat(), end=end.isoformat(),
                                       interval="1d", auto_adjust=True, timeout=15, raise_errors=True)
    rows = []
    for stamp, row in frame.iterrows():
        session = stamp.tz_convert("America/New_York") if stamp.tzinfo is not None else stamp
        rows.append({"session": session.date().isoformat(), **{key: float(row[key.title()]) for key in PRICE_FIELDS},
                     "volume": float(row["Volume"])})
    return rows


def _snapshot(rows: list[dict], *, sessions: list[date]) -> dict:
    dates = []
    clean = []
    allowed = set(sessions)
    try:
        for row in rows:
            day = date.fromisoformat(row["session"])
            if day > sessions[-1]:
                return {"failure_reason": "future_daily_bar"}
            if day not in allowed:
                return {"failure_reason": "unexpected_daily_session"}
            parsed = {key: float(row[key]) for key in (*PRICE_FIELDS, "volume")}
            if (not all(isfinite(value) for value in parsed.values()) or parsed["volume"] < 0
                or any(parsed[key] <= 0 for key in PRICE_FIELDS)
                or not parsed["low"] <= min(parsed["open"], parsed["close"])
                or not max(parsed["open"], parsed["close"]) <= parsed["high"]):
                return {"failure_reason": "invalid_daily_bar"}
            dates.append(day)
            clean.append({"session": day.isoformat(), **parsed})
    except (KeyError, OverflowError, TypeError, ValueError):
        return {"failure_reason": "invalid_daily_bar"}
    if len(dates) < 85:
        return {"failure_reason": "insufficient_completed_daily_history"}
    if dates[-1] != sessions[-1]:
        return {"failure_reason": "missing_latest_completed_daily_bar"}
    if dates != sessions[-len(dates):]:
        return {"failure_reason": "duplicate_unordered_or_missing_daily_sessions"}
    return {"bars": clean, "sha256": _digest(clean)}


async def prepare_inputs(*, entry_at: datetime, deadline: datetime, clock=_now, fetcher=_fetch_yfinance) -> dict:
    """Fetch six bounded daily snapshots. Persist this exact result before inference."""
    cutoff, day, sessions = input_window(entry_at, deadline)
    start = clock()
    if start < cutoff:
        raise SignalStudyError("The prior exchange session has not closed.")
    prepared = {
        "schema_version": 1, "entry_at": entry_at.isoformat(), "deadline": deadline.isoformat(),
        "input_cutoff": cutoff.isoformat(), "input_source": SOURCE, "inputs": {},
    }
    if start >= deadline:
        prepared["inputs"] = {ticker: {"failure_reason": "freeze_deadline_passed"} for ticker in TICKERS}
        return prepared
    try:
        spending, workflow = _runtime()
        prepared["settings"], _ = _configuration(spending, workflow)
        prepared["decision_source"] = _source_fingerprints()
    except SignalStudyError as exc:
        prepared["configuration_error"] = str(exc)
        prepared["inputs"] = {ticker: {"failure_reason": "research_backend_unavailable"} for ticker in TICKERS}
        return prepared
    prepared["engine"] = engine_identity(ROOT)

    async def fetch(ticker: str):
        try:
            timeout = min(20.0, (deadline - clock()).total_seconds())
            if timeout <= 0:
                return {"failure_reason": "freeze_deadline_passed"}
            rows = await asyncio.wait_for(asyncio.to_thread(fetcher, ticker, sessions[0], day), timeout=timeout)
        except TimeoutError:
            return {"failure_reason": "daily_source_timed_out"}
        except Exception:  # noqa: BLE001 - turn unreliable provider responses into explicit ticker gaps
            return {"failure_reason": "daily_source_unavailable"}
        received = clock()
        if received >= deadline:
            return {"failure_reason": "freeze_deadline_passed"}
        return {**_snapshot(rows, sessions=sessions), "retrieved_at": received.isoformat()}

    prepared["inputs"] = dict(zip(TICKERS, await asyncio.gather(*(fetch(ticker) for ticker in TICKERS))))
    prepared["prepared_at"] = clock().isoformat()
    return prepared


def _validated_snapshot(supplied: dict, sessions: list[date], cutoff: datetime, finish: datetime) -> dict:
    try:
        observed = datetime.fromisoformat(supplied["retrieved_at"])
        valid_time = cutoff <= observed <= finish
    except (KeyError, TypeError, ValueError):
        valid_time = False
    snapshot = _snapshot(supplied.get("bars", []), sessions=sessions)
    if not valid_time or "sha256" not in snapshot or snapshot["sha256"] != supplied.get("sha256"):
        raise SignalStudyError("The prepared snapshot is missing causal timing or its exact input fingerprint.")
    return snapshot


def _audit_client(client, agent: str, attempts: dict, deadline: datetime, clock) -> None:
    transport = client._client  # The SDK's HTTP client, using the same seam as the installed spending guard.
    if not getattr(transport, "_bta_spending_guard", False):
        raise SignalStudyError("The Luna client has no shared spending guard.")
    guarded = transport.send

    async def audited(request, **kwargs):
        if clock() >= deadline:
            raise SignalStudyError("freeze_deadline_passed")
        body = json.loads(request.content)
        messages = body.get("messages")
        if (str(request.url) != "https://api.openai.com/v1/chat/completions"
            or request.method != "POST" or body.get("model") != MODEL or not isinstance(messages, list)
            or body.get("stream")):
            raise SignalStudyError("Unsupported Luna study request.")
        attempt = {
            "agent": agent, "request_at": clock().isoformat(), "requested_model": MODEL,
            "settings": {key: value for key, value in body.items() if key != "messages"},
            "messages": messages, "prompt_sha256": _digest(messages), "returned_model": None,
        }
        attempts[ACTIVE_TICKER.get()].append(attempt)
        try:
            response = await guarded(request, **kwargs)
        except asyncio.CancelledError:
            attempt["outcome"] = "interrupted"
            raise
        except Exception:
            attempt["outcome"] = "failed"
            raise
        attempt.update(response_at=clock().isoformat(), http_status=response.status_code,
                       outcome="received" if response.is_success else "provider_error")
        if response.is_success:
            attempt["returned_model"] = response.json().get("model")
        return response

    transport.send = audited


def _client(workflow, configuration: dict, key: str, hook):
    extra = {"response_format": {"type": "json_object"}}
    if configuration["reasoning_effort"]:
        extra["reasoning_effort"] = configuration["reasoning_effort"]
    return workflow._make_llm(
        model=MODEL, base_url=configuration["base_url"], api_key=key, temperature=configuration["temperature"],
        timeout=configuration["timeout_seconds"], max_tokens=configuration["max_tokens"],
        reasoning_effort=configuration["reasoning_effort"], stream=False, additional_params=extra, client_hook=hook,
    )


def _market(ticker: str, snapshot: dict, cutoff: datetime):
    from app.tools.market_data import MarketData

    rows = snapshot["bars"]
    return MarketData(
        ticker=ticker, price=rows[-1]["close"], closes=[row["close"] for row in rows],
        highs=[row["high"] for row in rows], lows=[row["low"] for row in rows], volumes=[row["volume"] for row in rows],
        as_of=cutoff.isoformat(), history_as_of=rows[-1]["session"],
        sources={"prices": SOURCE, "forecast": "local"},
        # An injected empty forecast bypasses hosted lookup and takes the local fallback.
        hosted_forecast={},
    )


async def recover_costs(cohort_id: str) -> dict:
    """Recover charges for completed or interrupted attempts without rerunning them."""
    spending, _ = _runtime()
    return await asyncio.to_thread(spending.Ledger().receipt, f"prospective-{cohort_id}")


def _batch_cost(spending, batch: str) -> dict:
    try:
        return spending.Ledger().receipt(batch)
    except spending.BudgetError:
        return {"unresolved": True, "error": "shared_spending_receipt_unavailable"}


def _usable_response(record: dict, analysis) -> str | None:
    if (analysis.api_budget_blocked or analysis.error or analysis.technical is None or analysis.forecast is None
        or analysis.manager_decision is None):
        return "analysis_or_budget_failed"
    attempts = record["model_requests"]
    if len(attempts) != len(AGENTS) or any(r.get("returned_model") != MODEL or r.get("outcome") != "received" for r in attempts):
        return "model_identity_or_response_unverified"
    manager = record.get("raw_manager", {})
    raw_decision = str(manager.get("decision", "")).strip().upper()
    if manager.get("ticker") != record["ticker"] or raw_decision not in {"BUY", "HOLD", "SELL"}:
        return "invalid_manager_recommendation"
    for name in ("technical", "forecast"):
        result = record.get("agent_results", {}).get(name, {})
        if (result.get("ticker") != record["ticker"]
            or str(result.get("signal", "")).strip().lower() not in {"bullish", "bearish", "neutral"}):
            return "invalid_analyst_response"
    return None


async def freeze_signals(prepared: dict, *, cohort_id: str, entry_at: datetime, deadline: datetime, clock=_now) -> dict:
    """Run one already-claimed inference attempt. Never persists or reruns the claim."""
    cutoff, day, sessions = input_window(entry_at, deadline)
    started = clock()
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", cohort_id):
        raise SignalStudyError("Invalid study cohort identifier.")
    if (prepared.get("entry_at") != entry_at.isoformat() or prepared.get("deadline") != deadline.isoformat()
        or prepared.get("input_cutoff") != cutoff.isoformat() or prepared.get("input_source") != SOURCE):
        raise SignalStudyError("Prepared inputs do not match the enrolled cohort and its cutoff.")
    supplied = prepared.get("inputs", {})
    rows = [{"ticker": ticker, "fast": "FAILED", "momentum": None, "model_requests": [],
             "failure_reason": supplied.get(ticker, {}).get("failure_reason")} for ticker in TICKERS]
    outcome = {"status": "failed", "archive": None, "records": rows, "costs": None}
    if started >= deadline:
        outcome["status"] = "missed"
        for record in rows:
            record["failure_reason"] = record["failure_reason"] or "freeze_deadline_passed"
        return outcome
    try:
        spending, workflow = _runtime()
        cfg, keys = _configuration(spending, workflow)
        if prepared.get("settings") != cfg or prepared.get("decision_source") != _source_fingerprints():
            raise SignalStudyError("Frozen model settings or study policy changed since input preparation.")
        if prepared.get("engine") != engine_identity(ROOT):
            raise SignalStudyError("The installed simulator changed since input preparation.")
    except SignalStudyError as exc:
        outcome["error"] = str(exc)
        for record in rows:
            record["failure_reason"] = record["failure_reason"] or "research_backend_unavailable_or_changed"
        return outcome

    from app.backtest.grade import momentum_decision

    snapshots = {}
    for record in rows:
        ticker = record["ticker"]
        if record["failure_reason"]:
            continue
        try:
            snapshot = _validated_snapshot(supplied[ticker], sessions, cutoff, started)
            snapshots[ticker] = snapshot
            record["input_sha256"] = snapshot["sha256"]
            record["momentum"] = momentum_decision([row["close"] for row in snapshot["bars"]], OUTLOOK)
        except (KeyError, SignalStudyError):
            record["failure_reason"] = "frozen_daily_input_unverified"
    if len(snapshots) != len(TICKERS):
        outcome["status"] = "incomplete"
        for record in rows:
            record["failure_reason"] = record["failure_reason"] or "cohort_input_incomplete"
        return outcome
    batch = f"prospective-{cohort_id}"
    attempts = {record["ticker"]: record["model_requests"] for record in rows}

    async def analyze(record: dict):
        ticker = record["ticker"]
        if clock() >= deadline:
            return
        token = ACTIVE_TICKER.set(ticker)
        record["inference_started_at"] = clock().isoformat()

        async def emit(event: str, payload: dict):
            if event == "agent_completed":
                agent, result = payload.get("agent"), payload.get("result")
                if agent in AGENTS and isinstance(result, dict):
                    record.setdefault("agent_results", {})[agent] = result
                    if agent == "manager":
                        record["raw_manager"] = result

        try:
            with spending.batch_scope(f"{batch}:{ticker}"):
                analysis = await workflow.analyze_ticker(
                    ticker, emit, market_data=_market(ticker, snapshots[ticker], cutoff),
                    depth="fast", outlook=OUTLOOK, live_context=False, incognito=True, previous=None,
                )
            record["generated_at"] = clock().isoformat()
            record["risk_flags"] = analysis.risk_flags
            record["forecast_method"] = analysis.forecast_method
            record["failure_reason"] = _usable_response(record, analysis)
            if clock() >= deadline:
                record["failure_reason"] = "freeze_deadline_passed"
            if record["failure_reason"] is None:
                record["fast"] = analysis.decision
        except asyncio.CancelledError:
            record["failure_reason"] = "freeze_deadline_passed"
            raise
        except Exception:  # noqa: BLE001 - a failed ticker stays visible with any incurred charges
            record["failure_reason"] = "analysis_failed"
        finally:
            ACTIVE_TICKER.reset(token)

    try:
        async with AsyncExitStack() as clients, asyncio.timeout(max(0, (deadline - clock()).total_seconds())):
            pinned = {}
            for name in AGENTS:
                def hook(client, agent=name):
                    clients.push_async_callback(client.close)
                    _audit_client(client, agent, attempts, deadline, clock)

                pinned[name] = _client(workflow, cfg["agents"][name], keys[name], hook)
            with workflow.pinned_analysis_clients(pinned):
                # Preserve the workflow's process-wide semaphore. A conservative
                # cohort batch also limits queued work at a short freeze deadline.
                width = max(1, min(3, (cfg["llm_concurrency"] or 6) // 2))
                for offset in range(0, len(rows), width):
                    await asyncio.gather(*(analyze(row) for row in rows[offset:offset + width]))
    except TimeoutError:
        outcome["status"] = "missed"
    except Exception:  # noqa: BLE001 - retain any guarded charges from partial failures
        outcome["error"] = "Could not complete the pinned Luna analysis."
    finally:
        outcome["costs"] = await asyncio.to_thread(_batch_cost, spending, batch)

    for record in rows:
        ticker_costs = await asyncio.to_thread(_batch_cost, spending, f"{batch}:{record['ticker']}")
        record["costs"] = ticker_costs
        if record["fast"] == "FAILED" and not record["failure_reason"]:
            record["failure_reason"] = ("freeze_deadline_passed" if clock() >= deadline or outcome["status"] == "missed"
                                        else "analysis_failed")
        if record["fast"] != "FAILED" and (ticker_costs.get("reserved_usd") or ticker_costs.get("unresolved")
                                           or len(ticker_costs.get("requests", [])) != len(AGENTS)
                                           or any(receipt["state"] != "settled" for receipt in ticker_costs["requests"])):
            record["failure_reason"] = "research_cost_unresolved"
            record["fast"] = "FAILED"
    frozen_at = clock()
    if frozen_at >= deadline or outcome["status"] == "missed" or any(row["failure_reason"] == "freeze_deadline_passed" for row in rows):
        outcome["status"] = "missed"
        return outcome
    costs = outcome["costs"]
    if any(record["failure_reason"] for record in rows) or costs.get("unresolved") or costs.get("reserved_usd"):
        outcome["status"] = "incomplete"
        return outcome
    signals = [
        Signal(day=day, ticker=record["ticker"], fast=record["fast"], momentum=record["momentum"], input_cutoff=cutoff,
               generated_at=record["generated_at"], available_at=frozen_at,
               research_cost=Decimal(sum(item["settled_micro"] or 0 for item in record["costs"]["requests"])) / 1_000_000)
        for record in rows
    ]
    prompts = {record["ticker"]: record["model_requests"] for record in rows}
    outcome["archive"] = SignalArchive(source="imported_timestamped", candidate=CANDIDATE, model=MODEL, signals=signals,
        provenance={"input_source": SOURCE, "settings": cfg, "prompts": prompts, "prompt_sha256": _digest(prompts),
                    "inputs_sha256": _digest(prepared), "decision_policy_version": DECISION_POLICY,
                    "execution_policy_version": EXECUTION_POLICY_VERSION, "source_fingerprints": prepared["decision_source"],
                    "engine": prepared["engine"], "costs": costs,
                    "deployment_limitation": "The provider returned a rolling Luna alias and did not identify an immutable deployment."})
    outcome.update(status="signals_frozen", frozen_at=frozen_at.isoformat())
    return outcome
