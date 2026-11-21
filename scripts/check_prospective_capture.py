"""Offline capture checks using synthetic Webull responses and a simulated clock."""

import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta

import httpx

from app.backtest.options.domain import CHICAGO, TICKERS, Adjustment, Underlying, schedule
from app.backtest.options.prospective_capture import (
    MAX_REQUESTS_PER_MINUTE, RULES, AdjustmentCoveragePending, CohortQuotes, ContractTerm, MarketReference,
    _candidate_contracts, capture_entry, capture_exit, entry_dataset, replay_dataset,
)
from app.backtest.options.replay import EXIT_WINDOW, Tape
from app.backtest.options.webull import CONTRACTS_PATH, HOST, SNAPSHOTS_PATH, WebullError
from app.backtest.options.webull_credentials import WebullCredentials

DAY = date(2025, 11, 25)
ENTRY, EXIT = schedule(DAY, datetime.combine(DAY, time(8, 35), CHICAGO), RULES)
EXPIRATION = date(2025, 12, 12)
IDS = {ticker: f"{ticker}251212C00100000" for ticker in TICKERS}


class Clock:
    def __init__(self, at):
        self.at = at
        self.ticks = 0.0

    def now(self):
        return self.at + timedelta(seconds=self.ticks)

    def monotonic(self):
        return self.ticks

    async def sleep(self, seconds):
        self.ticks += seconds


def source_reference(known_at):
    return MarketReference(
        underlying=[Underlying(ticker=ticker, price=100, observed_at=ENTRY - timedelta(seconds=110),
                               available_at=ENTRY - timedelta(seconds=105)) for ticker in TICKERS],
        underlying_source="Independent offline fixture only",
        contract_terms=[ContractTerm(contract_id=IDS[ticker], expiration=EXPIRATION,
                                     last_trade_at=datetime.combine(EXPIRATION, time(15), CHICAGO),
                                     available_at=ENTRY - timedelta(days=1), source="Fixture product terms")
                        for ticker in TICKERS],
        adjustments_checked_through=known_at,
        adjustments_source="Offline fixture only",
    )


def listings(tickers):
    return [{"symbol": IDS[ticker], "underlying_symbol": ticker, "option_type": "CALL", "status": "LISTING",
             "expiration_date": EXPIRATION.isoformat(), "strike_price": "100.000", "instrument_id": str(i + 100),
             "multiplier": "100.000", "deliverables": [{"asset_type": "EQUITY", "symbol": ticker, "amount": "100"}]}
            for i, ticker in enumerate(TICKERS) if ticker in tickers]


def test_client(clock, mock):
    http = httpx.AsyncClient(transport=httpx.MockTransport(mock))
    client = CohortQuotes(http, WebullCredentials("fixture-key", "fixture-secret"), monotonic=clock.monotonic,
                          now=clock.now, sleep=clock.sleep)
    return http, client


async def check_campaign():
    calls = []
    clock = Clock(ENTRY - timedelta(seconds=100))
    source = source_reference(clock.now())

    async def mock(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == HOST
        if request.url.path == CONTRACTS_PATH:
            assert request.url.params["start_date"] == (DAY + timedelta(days=14)).isoformat()
            assert request.url.params["end_date"] == (DAY + timedelta(days=21)).isoformat()
            assert request.url.params["underlying_symbols"].split(",") == list(TICKERS)
            if request.url.params.get("pagination_key"):
                assert request.url.params["pagination_key"] == "opaque-test-cursor"
                return httpx.Response(200, json={"data": listings(TICKERS[3:])})
            return httpx.Response(200, json={"data": listings(TICKERS[:3]), "pagination_key": "opaque-test-cursor"})
        assert request.url.path == SNAPSHOTS_PATH
        wanted = request.url.params["symbols"].split(",")
        observed = int(clock.now().timestamp() * 1000)
        await clock.sleep(.1)
        return httpx.Response(200, json=[
            {"symbol": row["symbol"], "instrument_id": row["instrument_id"], "quote_time": observed,
             "bid": "1.05", "ask": "1.10", "bid_size": "5", "ask_size": "5"}
            for row in listings(TICKERS) if row["symbol"] in wanted])

    http, client = test_client(clock, mock)
    async with http:
        entry = await capture_entry(ENTRY, EXIT, source, client=client)
    assert [request.url.path for request in calls] == [CONTRACTS_PATH, CONTRACTS_PATH, SNAPSHOTS_PATH, SNAPSHOTS_PATH]
    assert "opaque-test-cursor" not in entry.model_dump_json()
    assert entry.covered_through == ENTRY + timedelta(seconds=3) and not entry.gaps
    assert len(entry.contracts) == len(entry.spots) == 6
    assert entry.batches[0]["window"] == "selection"
    assert entry.batches[1]["window"] == "entry_fill"
    assert all(not item["quality_flags"] for batch in entry.batches for item in batch["observations"])

    try:
        entry_dataset(entry, DAY, now=clock.now)
    except AdjustmentCoveragePending:
        pass
    else:
        raise AssertionError("pre-entry adjustment observations cannot cover the modeled fill")
    freeze = source.model_copy(update={"adjustments_checked_through": entry.covered_through})
    sealed = entry_dataset(entry, DAY, freeze, now=clock.now)
    assert sealed.source == "prospective" and sealed.resolution == "snapshot"
    assert not any(row.exit_quotes for row in sealed.coverage[DAY].values())
    assert sealed.quotes[0].available_at > sealed.quotes[0].observed_at
    substituted_reference = freeze.model_copy(update={"underlying": [], "contract_terms": []})
    assert entry_dataset(entry, DAY, substituted_reference, now=clock.now) == sealed
    unreported_entry_adjustment = Adjustment(contract_id=IDS["NVDA"], effective_at=ENTRY,
                                             evidence="Detected after the captured selection")
    try:
        entry_dataset(entry, DAY, freeze.model_copy(update={"adjustments": [unreported_entry_adjustment]}), now=clock.now)
    except ValueError as exc:
        assert "conflicts with the entry-time record" in str(exc)
    else:
        raise AssertionError("later evidence cannot change adjustment decisions at entry")
    for last_trade, expect_gap in ((entry.started_at - timedelta(seconds=1), True), (EXIT - timedelta(seconds=1), False)):
        terms = [term.model_copy(update={"last_trade_at": last_trade}) if term.contract_id == IDS["NVDA"] else term
                 for term in source.contract_terms]
        term_gaps = defaultdict(list)
        candidates = _candidate_contracts(entry.inventory, entry.spots, terms, entry.started_at, EXIT, term_gaps)
        assert IDS["NVDA"] not in {contract.id for contract in candidates}
        assert bool(term_gaps) == expect_gap
    late = {**entry.batches[0], "received_at": (ENTRY + timedelta(milliseconds=100)).isoformat()}
    shifted = entry.model_copy(update={"batches": [late, entry.batches[1]]})
    try:
        entry_dataset(shifted, DAY, freeze, now=clock.now)
    except ValueError as exc:
        assert "candidates lack timely selection evidence" in str(exc)
    else:
        raise AssertionError("provider quote time must not excuse a receipt after selection")
    tape = Tape(sealed)
    for contract in sealed.contracts:
        assert tape.at(contract.id, ENTRY)
        quote, reason = tape.entry_quote(contract, ENTRY, ENTRY + timedelta(seconds=1), ENTRY + timedelta(seconds=3))
        assert quote and reason is None

    clock = Clock(EXIT - timedelta(seconds=10))
    calls.clear()
    http, client = test_client(clock, mock)
    holds = [IDS["AMD"], IDS["META"]]
    async with http:
        exit = await capture_exit(EXIT, holds, freeze, client=client)
    assert exit.covered_through >= EXIT + EXIT_WINDOW
    assert calls and all(set(call.url.params["symbols"].split(",")) == set(holds) for call in calls)
    after = freeze.model_copy(update={"adjustments_checked_through": exit.covered_through})
    dataset = replay_dataset(entry, exit, DAY, after, sealed_entry=sealed, now=clock.now)
    assert list(dataset.quotes[:len(sealed.quotes)]) == sealed.quotes
    assert all(row.exit_quotes for row in dataset.coverage[DAY].values())
    assert Tape(dataset).exit_quote(Tape(dataset).contract_ids[holds[0]], EXIT, exit.covered_through, .01)
    missing = replay_dataset(entry, exit.model_copy(update={"batches": []}), DAY, after, sealed_entry=sealed, now=clock.now)
    assert not missing.coverage[DAY]["AMD"].exit_quotes and missing.coverage[DAY]["NVDA"].exit_quotes
    known_adjustment = Adjustment(contract_id=holds[0], effective_at=ENTRY + timedelta(days=1),
                                  evidence="Corporate action observed before exit capture")
    old_exit_reference = freeze.model_copy(update={"adjustments": [known_adjustment],
                                                  "adjustments_checked_through": exit.started_at})
    adjusted_exit = exit.model_copy(update={"reference": old_exit_reference})
    try:
        replay_dataset(entry, adjusted_exit, DAY, after, sealed_entry=sealed, now=clock.now)
    except ValueError as exc:
        assert "omits a held-contract adjustment recorded at exit" in str(exc)
    else:
        raise AssertionError("a replacement reference cannot erase recorded exit adjustments")
    amended = after.model_copy(update={"adjustments": [known_adjustment]})
    assert replay_dataset(entry, adjusted_exit, DAY, amended, sealed_entry=sealed, now=clock.now).adjustments == [
        known_adjustment,
    ]

    clock = Clock(EXIT - timedelta(seconds=10))
    calls.clear()
    http, client = test_client(clock, mock)
    async with http:
        empty = await capture_exit(EXIT, [], freeze, client=client)
    assert not calls and empty.covered_through == EXIT + EXIT_WINDOW
    assert replay_dataset(entry, empty, DAY, sealed_entry=sealed, now=clock.now).quotes == sealed.quotes


async def check_quota():
    clock = Clock(ENTRY)
    http, client = test_client(clock, lambda _: httpx.Response(200))
    async with http:
        for _ in range(MAX_REQUESTS_PER_MINUTE):
            await client._reserve_request(2)
        try:
            await client._reserve_request(2)
        except WebullError as exc:
            assert exc.code == "request_deadline_reached"
        else:
            raise AssertionError("batch must stop before exceeding quota")
        await client._reserve_request(65)
        assert clock.monotonic() == 60


asyncio.run(check_campaign())
asyncio.run(check_quota())
print("prospective Webull snapshot, evidence and sandbox quota checks passed")
