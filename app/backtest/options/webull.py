"""Collect a bounded sample of Webull sandbox option snapshots into local research storage."""

import argparse
import asyncio
import base64
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hmac
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.backtest.options.data import ROOT
from app.backtest.options.domain import CHICAGO, PRESETS, TICKERS, session_bounds
from app.backtest.options.webull_credentials import CredentialError, WebullCredentials, load_credentials, store_credentials

HOST = "api.sandbox.webull.com"
CONTRACTS_PATH = "/trading/instruments/options/contracts/list"
SNAPSHOTS_PATH = "/market-data/options/snapshots/list"
ALLOWED_PATHS = (CONTRACTS_PATH, SNAPSHOTS_PATH)
OUTPUT_ROOT = ROOT / "local-ops" / "backtests" / "options" / "prospective" / "webull" / "sandbox"
RULES = PRESETS["intraday_7_14_dte"]
CAPTURE_SECONDS = 300
CHECK_SECONDS = 45
SNAPSHOT_INTERVAL = 10
REQUEST_INTERVAL = 5  # At most 12 requests per minute, including retries, across both REST routes.
REQUEST_TIMEOUT = 5
SNAPSHOT_LIMIT = 20
CONTRACT_PAGE_LIMIT = 3
RECENT_SECONDS = 15
NUMERIC = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
OCC_CALL = re.compile(r"([A-Z]{1,6})([0-9]{6})C([0-9]{8})")
REQUIRED_FIELDS = ("bid", "ask", "bid_size", "ask_size", "provider_quote_time_ms")


class WebullError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def signed_headers(path: str, query: dict, credentials: WebullCredentials, timestamp: datetime, nonce: str) -> dict:
    if path not in ALLOWED_PATHS:
        raise WebullError("endpoint_not_allowlisted")
    # Webull signs the original parameter values before URL-encoding the joined string.
    headers = {"host": HOST, "x-app-key": credentials.app_key,
               "x-timestamp": timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "x-signature-version": "1.0", "x-signature-algorithm": "HMAC-SHA1", "x-signature-nonce": nonce}
    fields = {**query, **headers}
    canonical = "&".join(f"{key}={fields[key]}" for key in sorted(fields))
    message = quote(f"{path}&{canonical}", safe="").encode("utf-8")
    signature = hmac.digest(f"{credentials.app_secret}&".encode("utf-8"), message, "sha1")
    headers.update({"x-signature": base64.b64encode(signature).decode("ascii"), "x-version": "v3",
                    "accept": "application/json"})
    if credentials.access_token:
        headers["x-access-token"] = credentials.access_token
    return headers


def _retry_after(value: str | None, now: datetime) -> float:
    if not value:
        return 0
    if value.isascii() and value.isdecimal() and len(value) <= 9:
        return int(value)
    try:
        when = parsedate_to_datetime(value)
        return max(0, (when - now).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 0


class WebullQuotes:
    """The only network surface is two signed, sandbox-only GET endpoints."""

    def __init__(self, http: httpx.AsyncClient, credentials: WebullCredentials, *,
                 monotonic=time.monotonic, now=utc_now, sleep=asyncio.sleep):
        self.http = http
        self.credentials = credentials
        self.monotonic = monotonic
        self.now = now
        self.sleep = sleep
        self.next_request = 0.0

    async def _reserve_request(self, deadline: float):
        if max(self.monotonic(), self.next_request) >= deadline:
            raise WebullError("request_deadline_reached")
        if (pause := self.next_request - self.monotonic()) > 0:
            await self.sleep(pause)
        self.next_request = self.monotonic() + REQUEST_INTERVAL

    async def request(self, path: str, query: dict, deadline: float, attempts: list):
        if path not in ALLOWED_PATHS:
            raise WebullError("endpoint_not_allowlisted")
        if path == SNAPSHOTS_PATH:
            symbols = query.get("symbols", "").split(",")
            if (set(query) != {"symbols", "category"} or query["category"] != "US_OPTION"
                or not 1 <= len(symbols) <= SNAPSHOT_LIMIT or len(set(symbols)) != len(symbols)
                or any(not (match := OCC_CALL.fullmatch(symbol)) or match[1] not in TICKERS for symbol in symbols)):
                raise WebullError("invalid_snapshot_batch")
        for attempt in (1, 2):
            await self._reserve_request(deadline)
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise WebullError("request_deadline_reached")
            sent_at = self.now()
            headers = signed_headers(path, query, self.credentials, sent_at, uuid4().hex)
            event = {"attempt": attempt, "path": path,
                     "query": {key: value for key, value in query.items() if key != "pagination_key"},
                     "pagination_used": "pagination_key" in query, "requested_at": sent_at.isoformat()}
            attempts.append(event)
            try:
                response = await asyncio.wait_for(
                    self.http.get(f"https://{HOST}{path}", params=query, headers=headers, follow_redirects=False),
                    timeout=min(remaining, REQUEST_TIMEOUT),
                )
            except (httpx.RequestError, TimeoutError):
                code = "transport_error"
                retryable = True
                event["received_at"] = None
                event["finished_at"] = self.now().isoformat()
            else:
                received_at = self.now()
                event.update(received_at=received_at.isoformat(), http_status=response.status_code)
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        event["error"] = "invalid_json"
                        raise WebullError("invalid_json") from None
                    return payload, received_at
                if response.status_code in (401, 403):
                    code = "authentication_or_entitlement_rejected"
                elif response.status_code == 429:
                    code = "rate_limited"
                elif response.is_redirect:
                    code = "redirect_rejected"
                else:
                    code = "webull_http_error"
                retryable = response.status_code in (429, 500, 502, 503, 504)
                if retryable:
                    self.next_request = max(self.next_request, self.monotonic() +
                                            _retry_after(response.headers.get("retry-after"), received_at))
            event["error"] = code
            if attempt == 2 or not retryable or self.next_request >= deadline:
                raise WebullError(code)


def _number(value) -> Decimal | None:
    if type(value) not in (int, float, str):
        return None
    raw = str(value)
    return Decimal(raw) if len(raw) <= 32 and NUMERIC.fullmatch(raw) else None


def _displayed_size(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number == number.to_integral_value() else None


def _identifier(value) -> str | None:
    if type(value) not in (int, str):
        return None
    return str(value) if IDENTIFIER.fullmatch(str(value)) else None


def _quote_milliseconds(value) -> int | None:
    raw = str(value) if type(value) in (int, str) else ""
    if len(raw) != 13 or not raw.isascii() or not raw.isdecimal():
        return None
    milliseconds = int(raw)
    return milliseconds if 946684800000 <= milliseconds < 4102444800000 else None


def quote_observations(symbols: list[str], rows: list, received_at: datetime | None, previous: dict) -> list:
    observations = []
    for symbol in symbols:
        matches = [row for row in rows if isinstance(row, dict) and row.get("symbol") == symbol]
        row = matches[0] if len(matches) == 1 else {}
        flags = [] if row else ["duplicate_snapshot_rows" if matches else "missing_snapshot"]
        prices = {side: _number(row.get(side)) for side in ("bid", "ask")}
        sizes = {side: _displayed_size(row.get(side)) for side in ("bid_size", "ask_size")}
        source_time = _quote_milliseconds(row.get("quote_time"))
        fields = {**prices, **sizes, "quote_time": source_time}
        if row:
            flags.extend(f"{'missing' if row.get(key) in (None, '') else 'invalid'}_{key}"
                         for key, value in fields.items() if value is None)
        quote_age = None
        if source_time is not None and received_at is not None:
            age = received_at.timestamp() - source_time / 1000
            quote_age = round(age, 3)
            if age < 0:
                flags.append("quote_from_future")
            elif age > RECENT_SECONDS:
                flags.append("quote_stale")
            if symbol in previous:
                if source_time == previous[symbol]:
                    flags.append("quote_timestamp_unchanged")
                elif source_time < previous[symbol]:
                    flags.append("quote_timestamp_regressed")
            if age >= 0:
                previous[symbol] = max(previous.get(symbol, source_time), source_time)
        if None not in prices.values() and not 0 < prices["bid"] <= prices["ask"]:
            flags.append("invalid_two_sided_market")
        if 0 in sizes.values():
            flags.append("zero_displayed_size")
        delay = _displayed_size(row.get("delay_minutes"))
        if delay:
            flags.append("provider_reports_delay")
        observations.append({"symbol": symbol, "instrument_id": _identifier(row.get("instrument_id")),
                             **{key: str(value) if value is not None else None for key, value in prices.items()},
                             **sizes, "provider_quote_time_ms": source_time, "quote_age_seconds": quote_age,
                             "provider_delay_minutes": delay, "quality_flags": flags})
    return observations


def _contract(row: dict, ticker: str, day: date, *, rules=RULES) -> dict | None:
    symbol = row.get("symbol")
    match = OCC_CALL.fullmatch(symbol) if isinstance(symbol, str) else None
    if not match or match[1] != ticker or row.get("underlying_symbol") != ticker or row.get("option_type") != "CALL":
        return None
    try:
        expiration = date.fromisoformat(row.get("expiration_date", ""))
    except (ValueError, TypeError):
        return None
    dte = (expiration - day).days
    strike = _number(row.get("strike_price"))
    if (row.get("status") != "LISTING" or not rules.min_dte <= dte <= rules.max_dte or strike is None or strike <= 0
        or match[2] != expiration.strftime("%y%m%d") or Decimal(match[3]) / 1000 != strike):
        return None
    multiplier = _number(row.get("multiplier"))
    deliverables = row.get("deliverables")
    return {"symbol": symbol, "underlying": ticker, "expiration_date": expiration.isoformat(), "dte": dte,
            "option_type": "CALL", "status": "LISTING", "strike_price": str(strike), "dte_as_of": day.isoformat(),
            "instrument_id": _identifier(row.get("instrument_id")), "style": _identifier(row.get("style")),
            "multiplier": str(multiplier) if multiplier is not None else None,
            "deliverables": [
                {"asset_type": _identifier(item.get("asset_type")), "symbol": _identifier(item.get("symbol")),
                 "instrument_id": _identifier(item.get("instrument_id")),
                 "amount": str(amount) if (amount := _number(item.get("amount"))) is not None else None}
                for item in deliverables if isinstance(item, dict)
            ] if isinstance(deliverables, list) else None}


async def _discover(client: WebullQuotes, tickers: list[str], day: date, deadline: float, receipt: dict) -> list:
    contracts = {ticker: {} for ticker in tickers}
    query = {"underlying_symbols": ",".join(tickers), "category": "US_OPTION", "option_type": "CALL",
             "status": "LISTING", "show_deliverables": "true",
             # The sandbox returned an open-ended chain for start_date alone.
             "start_date": (day + timedelta(days=RULES.min_dte)).isoformat(),
             "end_date": (day + timedelta(days=RULES.max_dte)).isoformat()}
    cursors = set()
    cursor = None
    for _ in range(CONTRACT_PAGE_LIMIT):
        try:
            data, _ = await client.request(CONTRACTS_PATH, query, deadline, receipt["discovery_attempts"])
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise WebullError("unexpected_contract_response")
        except WebullError as exc:
            if not any(contracts.values()):
                raise
            receipt["discovery_error"] = exc.code
            break
        for ticker in tickers:
            for row in data["data"]:
                if isinstance(row, dict) and (contract := _contract(row, ticker, day)) is not None:
                    contracts[ticker].setdefault(contract["symbol"], contract)
        cursor = data.get("pagination_key")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            break
        cursors.add(cursor)
        query["pagination_key"] = cursor
    if cursor:
        receipt["flags"].append("contract_page_truncated")
    chosen = []
    for candidates in contracts.values():
        if not candidates:
            continue
        # Probe the middle listed strike of the expiration closest to the target DTE.
        expiry = min(candidates.values(), key=lambda c: (abs(c["dte"] - RULES.target_dte), c["expiration_date"]))["expiration_date"]
        same_expiry = sorted((c for c in candidates.values() if c["expiration_date"] == expiry),
                             key=lambda c: (Decimal(c["strike_price"]), c["symbol"]))
        chosen.append(same_expiry[len(same_expiry) // 2])
    receipt["underlyings_without_contract"] = [ticker for ticker, candidates in contracts.items() if not candidates]
    return chosen


@contextmanager
def capture_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    # Keep the lock file in place so a subsequent process never locks a different file identity.
    with (root / "capture.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WebullError("capture_already_running") from None
        yield


def _market_open(at: datetime) -> bool:
    bounds = session_bounds(at)
    return bool(bounds and bounds[0] <= at < bounds[1])


def _receipt(output: Path, receipt: dict) -> None:
    staged = output / "receipt.tmp"
    staged.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    staged.replace(output / "receipt.json")


async def _sample(client: WebullQuotes, symbols: list[str], deadline: float, previous: dict, batch_id: int) -> dict:
    batch = {"batch_id": batch_id, "requested_symbols": symbols, "attempts": []}
    rows = []
    received_at = None
    try:
        data, received_at = await client.request(SNAPSHOTS_PATH, {"symbols": ",".join(symbols), "category": "US_OPTION"},
                                                 deadline, batch["attempts"])
        if not isinstance(data, list):
            raise WebullError("unexpected_snapshot_response")
        rows = data
    except WebullError as exc:
        batch["error"] = exc.code
    batch.update(observations=quote_observations(symbols, rows, received_at, previous),
                 request_at=batch["attempts"][0]["requested_at"] if batch["attempts"] else None,
                 received_at=received_at.isoformat() if received_at is not None else None,
                 market_open_at_receipt=_market_open(received_at) if received_at is not None else None,
                 unexpected_rows=sum(not isinstance(row, dict) or row.get("symbol") not in symbols for row in rows))
    return batch


async def collect(verb: str, tickers: list[str], output: Path, *, http: httpx.AsyncClient,
                  credentials: WebullCredentials | None, monotonic=time.monotonic, now=utc_now,
                  sleep=asyncio.sleep, credential_error: str | None = None) -> dict:
    if verb not in ("check", "capture") or not 1 <= len(set(tickers)) == len(tickers) <= 2 or not set(tickers) <= set(TICKERS):
        raise WebullError("invalid_sample_request")
    started = now()
    lifetime = CAPTURE_SECONDS if verb == "capture" else CHECK_SECONDS
    deadline = monotonic() + lifetime
    market_open = _market_open(started)
    receipt = {"schema_version": 1, "recording_kind": "sampled_option_snapshots", "purpose": verb,
               "environment": "sandbox", "endpoint": f"https://{HOST}", "data_environment": None,
               "started_at": started.isoformat(), "market_open_at_start": market_open,
               "requested_underlyings": tickers, "contracts": [], "discovery_attempts": [], "flags": [],
               "connection_verified": False, "required_quote_fields_complete": False, "snapshot_batches": 0,
               "gap_batches": 0, "freshness_status": "not_verified" if market_open else "outside_regular_session",
               "freshness_threshold_seconds": RECENT_SECONDS, "maximum_duration_seconds": lifetime,
               "snapshot_interval_seconds": SNAPSHOT_INTERVAL}
    output.mkdir(parents=True, exist_ok=True)
    receipt["status"] = "started"
    _receipt(output, receipt)
    try:
        if credentials is None:
            receipt.update(status="blocked", error=credential_error or "sandbox_credentials_missing")
            return receipt
        if verb == "capture":
            bounds = session_bounds(started)
            if not market_open or started + timedelta(seconds=lifetime) > bounds[1]:
                receipt.update(status="blocked", error="five_regular_session_minutes_required")
                return receipt
        client = WebullQuotes(http, credentials, monotonic=monotonic, now=now, sleep=sleep)
        receipt["contracts"] = await _discover(client, tickers, started.astimezone(CHICAGO).date(), deadline, receipt)
        if not receipt["contracts"]:
            raise WebullError("no_listed_call_within_sample_dte")
        symbols = [contract["symbol"] for contract in receipt["contracts"]]
        identities = {contract["symbol"]: contract["instrument_id"] for contract in receipt["contracts"]}
        previous = {}
        quality = Counter()
        with (output / "snapshots.jsonl").open("x", encoding="utf-8") as stream:
            receipt["snapshot_file"] = "snapshots.jsonl"
            while monotonic() < deadline:
                batch_start = monotonic()
                batch = await _sample(client, symbols, deadline, previous, receipt["snapshot_batches"] + 1)
                observations = batch["observations"]
                bounds = session_bounds(datetime.fromisoformat(batch["received_at"])) if batch["market_open_at_receipt"] else None
                for row in observations:
                    expected_id = identities[row["symbol"]]
                    if expected_id and row["instrument_id"] and row["instrument_id"] != expected_id:
                        row["quality_flags"].append("instrument_id_mismatch")
                    source_time = row["provider_quote_time_ms"]
                    if bounds and source_time is not None and source_time < bounds[0].timestamp() * 1000:
                        row["quality_flags"].append("quote_before_regular_session")
                    if bounds and not row["quality_flags"]:
                        receipt["freshness_status"] = "recent_provider_quote_observed"
                    quality.update(row["quality_flags"])
                stream.write(json.dumps(batch, separators=(",", ":")) + "\n")
                stream.flush()
                receipt["snapshot_batches"] += 1
                # A parsed HTTP 200 array is evidence that the signed snapshot request was accepted.
                if "error" not in batch:
                    receipt["connection_verified"] = True
                if any(any(row[key] is not None for key in REQUIRED_FIELDS) for row in observations):
                    receipt["data_environment"] = "sandbox"
                complete = all(all(row[key] is not None for key in REQUIRED_FIELDS)
                               and "instrument_id_mismatch" not in row["quality_flags"] for row in observations)
                complete = complete and not batch["unexpected_rows"]
                if not complete:
                    receipt["gap_batches"] += 1
                receipt["quality_flag_counts"] = dict(sorted(quality.items()))
                if verb == "check" or batch.get("error") in ("authentication_or_entitlement_rejected", "redirect_rejected"):
                    if "error" in batch:
                        receipt["error"] = batch["error"]
                    break
                await sleep(max(0, min(deadline, batch_start + SNAPSHOT_INTERVAL) - monotonic()))
        receipt["required_quote_fields_complete"] = (receipt["snapshot_batches"] > 0 and receipt["gap_batches"] == 0
                                                     and len(symbols) == len(tickers))
        receipt["status"] = ("complete" if receipt["required_quote_fields_complete"]
                             else "partial" if receipt["connection_verified"] else "failed")
        return receipt
    except WebullError as exc:
        receipt.update(status="failed", error=exc.code)
        return receipt
    except asyncio.CancelledError:
        receipt.update(status="interrupted", error="capture_interrupted")
        raise
    finally:
        receipt["finished_at"] = now().isoformat()
        _receipt(output, receipt)


async def _run(verb: str, tickers: list[str]) -> int:
    creds = error = None
    try:
        creds = load_credentials()
    except CredentialError as exc:
        error = exc.reason
    with capture_lock(OUTPUT_ROOT):
        output = OUTPUT_ROOT / uuid4().hex
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False, follow_redirects=False) as http:
            receipt = await collect(verb, tickers, output, http=http, credentials=creds, credential_error=error)
    print(f"{receipt['status']} | {receipt.get('error', receipt['freshness_status'])} | {output / 'receipt.json'}")
    return int(receipt["status"] != "complete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="verb", required=True)
    subcommands.add_parser("credentials", help="store the existing sandbox application credentials in Windows Credential Manager")
    for verb in ("check", "capture"):
        command = subcommands.add_parser(verb)
        command.add_argument("--environment", choices=("sandbox",), required=True)
        command.add_argument("--symbol", action="append", choices=TICKERS, help="one or two underlyings (default: AAPL and NVDA)")
    args = parser.parse_args()
    try:
        if args.verb == "credentials":
            store_credentials()
            print("Webull sandbox application credentials saved in Windows Credential Manager.")
            return
        tickers = args.symbol or ["AAPL", "NVDA"]
        if not 1 <= len(tickers) <= 2 or len(set(tickers)) != len(tickers):
            parser.error("select one or two distinct underlyings")
        raise SystemExit(asyncio.run(_run(args.verb, tickers)))
    except (WebullError, CredentialError) as exc:
        parser.exit(1, f"{exc}\n")
    except OSError:
        parser.exit(1, "Unable to read or write the local Webull recording.\n")
    except KeyboardInterrupt:
        parser.exit(130, "Webull snapshot collection was interrupted.\n")


if __name__ == "__main__":
    main()
