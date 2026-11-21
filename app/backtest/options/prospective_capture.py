"""Capture bounded Webull sandbox snapshots without inventing unobserved evidence."""

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

import httpx
from pydantic import AwareDatetime, Field, model_validator

from app.backtest.options.domain import (
    CHICAGO, PRESETS, TICKERS, Adjustment, Contract, Coverage, Dataset, Execution, Quote, Record, Underlying,
    schedule, session_bounds,
)
from app.backtest.options.replay import EXIT_WINDOW, TICK_ENTRY_DELAY, Tape
from app.backtest.options.webull import (
    CONTRACTS_PATH, HOST, OCC_CALL, OUTPUT_ROOT, REQUEST_TIMEOUT, SNAPSHOT_LIMIT, WebullError, WebullQuotes,
    _contract, _sample, capture_lock, utc_now,
)
from app.backtest.options.webull_credentials import CredentialError, load_credentials

RULES = PRESETS["swing_5_sessions"]
MAX_REQUESTS_PER_MINUTE = 24  # Conservative shared budget below the sandbox's 30/60s per-route quota.
MAX_CHAIN_PAGES = 8
SPOT_FRESHNESS = timedelta(minutes=2)
QUOTE_FRESHNESS = timedelta(seconds=2)
EXIT_LATE_WINDOW = timedelta(seconds=30)
CAPTURE_METHOD = "Webull sandbox polled option snapshots"


class AdjustmentCoveragePending(ValueError):
    code = "adjustment_coverage_pending"


class ContractTerm(Record):
    contract_id: str = Field(min_length=1, max_length=80)
    expiration: date
    last_trade_at: AwareDatetime
    available_at: AwareDatetime
    source: str = Field(min_length=1, max_length=512)


class MarketReference(Record):
    """Independent spot, final trading deadline and corporate-action evidence."""

    underlying: list[Underlying] = Field(default_factory=list)
    underlying_source: str = Field(min_length=1, max_length=512)
    contract_terms: list[ContractTerm] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    adjustments_checked_through: AwareDatetime
    adjustments_source: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def check_evidence(self):
        if len({term.contract_id for term in self.contract_terms}) != len(self.contract_terms):
            raise ValueError("duplicate contract reference terms")
        if any(row.ticker not in TICKERS for row in self.underlying):
            raise ValueError("underlying reference contains a symbol outside the study")
        if any(row.effective_at > self.adjustments_checked_through for row in self.adjustments):
            raise ValueError("adjustment is outside the independently checked window")
        return self


class EntryEvidence(Record):
    entry_at: AwareDatetime
    exit_at: AwareDatetime
    started_at: AwareDatetime
    covered_through: AwareDatetime
    reference: MarketReference
    spots: list[Underlying]
    contracts: list[Contract]
    inventory: list[dict]
    discovery_attempts: list[dict]
    batches: list[dict]
    gaps: dict[str, list[str]]
    provider: Literal["api.sandbox.webull.com"] = HOST
    method: Literal["Webull sandbox polled option snapshots"] = CAPTURE_METHOD


class ExitEvidence(Record):
    exit_at: AwareDatetime
    started_at: AwareDatetime
    covered_through: AwareDatetime
    contract_ids: list[str]
    reference: MarketReference
    batches: list[dict]
    gaps: list[str]
    provider: Literal["api.sandbox.webull.com"] = HOST
    method: Literal["Webull sandbox polled option snapshots"] = CAPTURE_METHOD


class CohortQuotes(WebullQuotes):
    """Reserve attempts before send, including retries, across discovery and quote batches."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attempt_times = deque()

    async def _reserve_request(self, deadline: float):
        while True:
            now = self.monotonic()
            while self.attempt_times and self.attempt_times[0] <= now - 60:
                self.attempt_times.popleft()
            next_slot = max(now, self.next_request)
            if len(self.attempt_times) >= MAX_REQUESTS_PER_MINUTE:
                next_slot = max(next_slot, self.attempt_times[0] + 60)
            if next_slot >= deadline:
                raise WebullError("request_deadline_reached")
            if next_slot <= now:
                self.attempt_times.append(now)
                return
            await self.sleep(next_slot - now)


@asynccontextmanager
async def _client(provided):
    if provided is not None:
        yield provided
        return
    with capture_lock(OUTPUT_ROOT):
        try:
            credentials = load_credentials()
        except CredentialError as exc:
            raise WebullError(exc.reason) from None
        if credentials is None:
            raise WebullError("sandbox_credentials_missing")
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False, follow_redirects=False) as http:
            yield CohortQuotes(http, credentials)


def _deadline(client: WebullQuotes, at: datetime) -> float:
    return client.monotonic() + max(0, (at - client.now()).total_seconds())


async def _wait_until(client: WebullQuotes, at: datetime):
    await client.sleep(max(0, (at - client.now()).total_seconds()))


def _select_spots(reference: MarketReference, entry_at: datetime, known_at: datetime):
    spots, gaps = [], defaultdict(list)
    for ticker in TICKERS:
        candidates = [row for row in reference.underlying
                      if row.ticker == ticker and entry_at - SPOT_FRESHNESS <= row.observed_at
                      <= row.available_at <= min(entry_at, known_at)]
        if not candidates:
            gaps[ticker].append("no timely independent underlying observation")
            continue
        latest = max(candidates, key=lambda row: row.chronological_key)
        if len({r.price for r in candidates if r.chronological_key == latest.chronological_key}) != 1:
            gaps[ticker].append("conflicting underlying observations")
            continue
        spots.append(latest)
    return spots, gaps


async def _discover(client: WebullQuotes, entry_at: datetime, cutoff: datetime, attempts: list):
    day = entry_at.astimezone(CHICAGO).date()
    query = {"underlying_symbols": ",".join(TICKERS), "category": "US_OPTION", "option_type": "CALL",
             "status": "LISTING", "show_deliverables": "true",
             "start_date": (day + timedelta(days=RULES.min_dte)).isoformat(),
             "end_date": (day + timedelta(days=RULES.max_dte)).isoformat()}
    inventory = []
    seen_pages = set()
    seen_listings = {}
    for _ in range(MAX_CHAIN_PAGES):
        try:
            data, received = await client.request(CONTRACTS_PATH, query, _deadline(client, cutoff), attempts)
        except WebullError as exc:
            return inventory, f"option-chain request did not complete: {exc.code}"
        if received > cutoff or not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return inventory, "option chain arrived late or the response format is unsupported"
        for row in data["data"]:
            if not isinstance(row, dict) or (ticker := row.get("underlying_symbol")) not in TICKERS:
                return inventory, "unexpected listing in the option chain"
            contract = _contract(row, ticker, day, rules=RULES)
            if contract is None:
                return inventory, "option-chain listing cannot be verified in the declared call expiration range"
            if seen_listings.setdefault(contract["symbol"], contract) != contract:
                return inventory, "conflicting instrument identity or terms across listing pages"
            inventory.append({**contract, "listed_observation_at": received.isoformat()})
        cursor = data.get("pagination_key")
        if cursor in (None, ""):
            return inventory, None
        if not isinstance(cursor, str) or cursor in seen_pages:
            return inventory, "option-chain pagination is ambiguous"
        seen_pages.add(cursor)
        query["pagination_key"] = cursor
    return inventory, "option-chain pagination did not complete before the bounded page limit"


def _standard(row: dict) -> bool | None:
    deliverables = row["deliverables"]
    if not row["multiplier"] or not deliverables or any(
        not all(delivery.get(key) for key in ("asset_type", "symbol", "amount")) for delivery in deliverables
    ):
        return None
    if Decimal(row["multiplier"]) != 100 or len(deliverables) != 1:
        return False
    delivery = deliverables[0]
    return (delivery["asset_type"].upper() in ("STOCK", "EQUITY") and delivery["symbol"] == row["underlying"]
            and Decimal(delivery["amount"]) == 100)


def _candidate_contracts(inventory, spots, terms, known_at, exit_at, gaps):
    spot_by_ticker = {row.ticker: row.price for row in spots}
    terms = {term.contract_id: term for term in terms if term.available_at <= known_at}
    listed = set()
    candidates = {}
    for row in inventory:
        ticker, symbol = row["underlying"], row["symbol"]
        listed.add(ticker)
        spot = spot_by_ticker.get(ticker)
        strike = Decimal(row["strike_price"])
        if spot is None or abs(strike - spot) / spot > Decimal("0.02"):
            continue
        standard = _standard(row)
        if standard is False:
            continue
        if standard is None or not row["instrument_id"]:
            gaps[ticker].append(f"{symbol}: unverified instrument or deliverables")
            continue
        term = terms.get(symbol)
        expiration = date.fromisoformat(row["expiration_date"])
        if term is None or term.expiration != expiration:
            gaps[ticker].append(f"{symbol}: missing independent last-trade reference")
            continue
        try:
            contract = Contract(id=symbol, ticker=ticker, right="call", strike=strike, expiration=expiration,
                                last_trade_at=term.last_trade_at, multiplier=100, deliverable="standard_100_shares",
                                listed_at=row["listed_observation_at"])
        except ValueError:
            gaps[ticker].append(f"{symbol}: invalid listing or independently supplied last-trade timing")
            continue
        if term.last_trade_at <= exit_at or exit_at.astimezone(CHICAGO).date() >= expiration:
            continue
        if (previous := candidates.setdefault(symbol, contract)) != contract:
            if previous.model_dump(exclude={"listed_at"}) != contract.model_dump(exclude={"listed_at"}):
                gaps[ticker].append(f"{symbol}: conflicting contract metadata across pages")
    for ticker in TICKERS:
        if ticker not in listed:
            gaps[ticker].append("no option-chain listings returned for the underlying")
    return sorted(candidates.values(), key=lambda c: c.id)


async def _snapshot_window(client, ids, identities, label, at, cutoff, sequence, previous):
    await _wait_until(client, at)
    groups = [ids[i:i + SNAPSHOT_LIMIT] for i in range(0, len(ids), SNAPSHOT_LIMIT)]
    batches = await asyncio.gather(*(
        _sample(client, group, _deadline(client, cutoff), previous, sequence + i)
        for i, group in enumerate(groups)
    ))
    for batch in batches:
        batch["window"] = label
        for quote in batch["observations"]:
            instrument = identities.get(quote["symbol"])
            if instrument is not None and instrument != quote["instrument_id"]:
                quote["quality_flags"].append("instrument_id_mismatch")
    return batches


async def capture_entry(entry_at: datetime, exit_at: datetime, reference: MarketReference, *, client=None) -> EntryEvidence:
    """Discover the complete, independently supported candidate set before polling."""
    clock = client.now if client is not None else utc_now
    started = clock()
    if started.tzinfo is None or entry_at.tzinfo is None or exit_at.tzinfo is None:
        raise ValueError("capture times must be timezone-aware")
    day = entry_at.astimezone(CHICAGO).date()
    cutoff = entry_at - QUOTE_FRESHNESS
    if schedule(day, entry_at - timedelta(seconds=1), RULES) != (entry_at, exit_at) or not (
        entry_at - SPOT_FRESHNESS <= started < cutoff
    ):
        raise ValueError("start entry capture within two minutes of the frozen exchange-calendar entry")
    if reference.adjustments_checked_through > started:
        raise ValueError("independent adjustment evidence cannot extend beyond the start of capture")
    spots, gaps = _select_spots(reference, entry_at, started)
    inventory, attempts, batches, contracts = [], [], [], []
    try:
        if not gaps:
            async with _client(client) as active:
                inventory, issue = await _discover(active, entry_at, cutoff, attempts)
                if issue:
                    gaps["chain"].append(issue)
                contracts = _candidate_contracts(inventory, spots, reference.contract_terms, started, exit_at, gaps)
                if not gaps and contracts:
                    ids = [contract.id for contract in contracts]
                    groups = (len(ids) + SNAPSHOT_LIMIT - 1) // SNAPSHOT_LIMIT
                    if len(attempts) + 2 * groups > MAX_REQUESTS_PER_MINUTE:
                        gaps["capture"].append("complete candidate batches exceed the bounded Webull request budget")
                    else:
                        identities = {row["symbol"]: row["instrument_id"] for row in inventory}
                        previous = {}
                        batches.extend(await _snapshot_window(active, ids, identities, "selection",
                                                              entry_at - timedelta(milliseconds=1700), entry_at, 1, previous))
                        batches.extend(await _snapshot_window(active, ids, identities, "entry_fill",
                                                              entry_at + timedelta(seconds=1), entry_at + TICK_ENTRY_DELAY,
                                                              len(batches) + 1, previous))
                        await _wait_until(active, entry_at + TICK_ENTRY_DELAY)
                elif not gaps:
                    await _wait_until(active, entry_at + TICK_ENTRY_DELAY)
    except WebullError as exc:
        gaps["capture"].append(exc.code)
    return EntryEvidence(entry_at=entry_at, exit_at=exit_at, started_at=started, covered_through=clock(),
                         reference=reference, spots=spots, contracts=contracts, inventory=inventory,
                         discovery_attempts=attempts, batches=batches, gaps=dict(gaps))


async def capture_exit(exit_at: datetime, contract_ids, reference: MarketReference, *, client=None) -> ExitEvidence:
    """Poll only the contracts sealed at entry, through the scheduled mark."""
    clock = client.now if client is not None else utc_now
    started = clock()
    if exit_at.tzinfo is None or started.tzinfo is None:
        raise ValueError("capture times must be timezone-aware")
    bounds = session_bounds(exit_at)
    mark_at = exit_at + EXIT_WINDOW
    end = mark_at + EXIT_LATE_WINDOW
    ids = sorted(set(contract_ids))
    if len(ids) > SNAPSHOT_LIMIT or any(not (match := OCC_CALL.fullmatch(symbol)) or match[1] not in TICKERS for symbol in ids):
        raise ValueError("exit capture accepts only the sealed long-call contracts")
    if not bounds or exit_at != bounds[1] - timedelta(minutes=15) or not (exit_at - timedelta(seconds=30) <= started < mark_at):
        raise ValueError("exit capture must start in the scheduled exit window")
    if reference.adjustments_checked_through > started:
        raise ValueError("independent adjustment evidence cannot extend beyond the start of capture")
    batches, gaps = [], []
    if ids:
        try:
            async with _client(client) as active:
                # A pre-mark batch gives a timely source quote a chance to arrive before the mark.
                targets = [exit_at + timedelta(seconds=offset) for offset in range(0, 150, 10)]
                targets.append(mark_at - timedelta(milliseconds=1700))
                previous = {}
                for stamp in sorted(targets):
                    if active.now() >= end or stamp < started - timedelta(seconds=1):
                        continue
                    batches.extend(await _snapshot_window(active, ids, {}, "exit", stamp,
                                                          min(stamp + timedelta(seconds=3), end), len(batches) + 1, previous))
                await _wait_until(active, end)
        except WebullError as exc:
            gaps.append(exc.code)
    elif client is not None:
        await _wait_until(client, mark_at)
    else:
        await asyncio.sleep(max(0, (mark_at - utc_now()).total_seconds()))
    return ExitEvidence(exit_at=exit_at, started_at=started, covered_through=clock(), contract_ids=ids,
                        reference=reference, batches=batches, gaps=gaps)


def _normalize_quotes(batches, allowed, start, cutoff, identities):
    quotes = []
    seen = {}
    for batch in batches:
        if not batch["received_at"] or not batch["request_at"] or batch.get("error"):
            continue
        received = datetime.fromisoformat(batch["received_at"])
        request = datetime.fromisoformat(batch["request_at"])
        if not start <= request <= received <= cutoff:
            continue
        for row in batch["observations"]:
            if row["symbol"] not in allowed or row["instrument_id"] != identities[row["symbol"]]:
                continue
            if any(row.get(key) is None for key in ("provider_quote_time_ms", "bid", "ask", "bid_size", "ask_size")):
                continue
            if {"provider_reports_delay", "quote_from_future", "instrument_id_mismatch", "quote_timestamp_regressed"} & set(row["quality_flags"]):
                continue
            observed = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=row["provider_quote_time_ms"])
            try:
                quote = Quote(contract_id=row["symbol"], observed_at=observed, available_at=received,
                              source_timestamp=observed.isoformat(), bid=row["bid"], ask=row["ask"],
                              bid_size=row["bid_size"], ask_size=row["ask_size"])
            except ValueError:
                continue  # The unusable price or timestamp remains in the raw capture.
            book = (quote.bid, quote.ask, quote.bid_size, quote.ask_size)
            if seen.setdefault((quote.contract_id, quote.observed_at), book) != book:
                raise ValueError("conflicting polled quotes share a contract and provider timestamp")
            quotes.append(quote)
    return quotes


def _adjustments(reference: MarketReference, ids, through: datetime, now):
    if reference.adjustments_checked_through > now():
        raise ValueError("independent adjustment evidence extends into the future")
    if reference.adjustments_checked_through < through:
        raise AdjustmentCoveragePending("independent adjustment evidence must cover the observed window")
    return [row for row in reference.adjustments if row.contract_id in ids and row.effective_at <= through]


def entry_dataset(evidence: EntryEvidence, day: date, reference: MarketReference | None = None, *, now=utc_now) -> Dataset:
    """Normalize exactly the sealed selection and fill windows, with no exit coverage."""
    if evidence.gaps:
        details = "; ".join(f"{key}: {', '.join(values[:3])}" for key, values in evidence.gaps.items())
        raise ValueError("entry capture incomplete: " + details)
    fill = evidence.entry_at + TICK_ENTRY_DELAY
    if evidence.covered_through < fill:
        raise ValueError("entry capture ended before the modeled fill")
    ids = {contract.id for contract in evidence.contracts}
    identities = {row["symbol"]: row["instrument_id"] for row in evidence.inventory}
    adjustments = _adjustments(reference or evidence.reference, ids, fill, now)
    known = {row for row in evidence.reference.adjustments if row.contract_id in ids and row.effective_at <= fill}
    if set(adjustments) != known:
        raise ValueError("new corporate-action evidence conflicts with the entry-time record")
    quotes = _normalize_quotes(evidence.batches, ids, evidence.entry_at - QUOTE_FRESHNESS, fill, identities)
    dataset = Dataset(schema_version=1, preset=RULES.preset, source="prospective", resolution="snapshot",
                      provider=HOST, entitlement=CAPTURE_METHOD, covered_through=min(evidence.covered_through, fill),
                      coverage={day: {ticker: Coverage(discovery=True, entry_quotes=True, exit_quotes=False,
                                                      underlying=True, adjustments=True) for ticker in TICKERS}},
                      contracts=evidence.contracts, underlying=evidence.spots, quotes=quotes, adjustments=adjustments)
    tape = Tape(dataset)
    missing = [contract.id for contract in dataset.contracts if tape.at(contract.id, evidence.entry_at) is None]
    if missing:
        raise ValueError(f"{len(missing)} candidates lack timely selection evidence: {', '.join(missing[:4])}")
    return dataset


def replay_dataset(entry: EntryEvidence, exit: ExitEvidence, day: date, reference: MarketReference | None = None,
                   *, sealed_entry: Dataset | None = None, now=utc_now) -> Dataset:
    """Add the exit window to the sealed entry without changing any entry input."""
    initial = sealed_entry if sealed_entry is not None else entry_dataset(entry, day, reference, now=now)
    mark_at = entry.exit_at + EXIT_WINDOW
    if exit.exit_at != entry.exit_at or exit.started_at < entry.exit_at - timedelta(seconds=30):
        raise ValueError("exit capture does not match the frozen fifth session")
    if exit.covered_through < mark_at or exit.covered_through > now():
        raise ValueError("scheduled comparison evidence is still pending")
    ids = {contract.id for contract in initial.contracts}
    if not set(exit.contract_ids) <= ids:
        raise ValueError("exit evidence names a contract outside the sealed entry set")
    identities = {row["symbol"]: row["instrument_id"] for row in entry.inventory}
    # An all-cash entry has no market orders or adjustment exposure at exit.
    changes = _adjustments(reference or exit.reference, ids, exit.covered_through, now) if exit.contract_ids else initial.adjustments
    known_at_entry = {row for row in changes if row.effective_at <= initial.covered_through}
    if known_at_entry != set(initial.adjustments):
        raise ValueError("new corporate-action evidence conflicts with the sealed entry")
    known_at_exit = {row for row in exit.reference.adjustments if row.contract_id in exit.contract_ids
                     and row.effective_at <= exit.covered_through}
    if not known_at_exit.issubset(changes):
        raise ValueError("final corporate-action evidence omits a held-contract adjustment recorded at exit")
    quotes = _normalize_quotes(exit.batches, set(exit.contract_ids), exit.exit_at, exit.covered_through, identities)
    combined = Dataset.model_validate({**initial.model_dump(), "covered_through": exit.covered_through,
                                       "adjustments": changes, "quotes": [*initial.quotes, *quotes]})
    tape = Tape(combined)
    missing = defaultdict(list)
    for symbol in exit.contract_ids:
        contract = tape.contract_ids[symbol]
        if tape.exit_quote(contract, exit.exit_at, exit.covered_through, Execution().stress_slippage) is None:
            missing[contract.ticker].append(symbol)
    return combined.model_copy(update={"coverage": {day: {
        ticker: Coverage(discovery=True, entry_quotes=True, exit_quotes=not missing[ticker],
                         underlying=True, adjustments=True,
                         note="; ".join([*exit.gaps, *(f"{symbol}: no qualifying exit snapshot" for symbol in missing[ticker])]))
        for ticker in TICKERS
    }}})
