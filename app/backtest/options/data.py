"""Local normalized market-data adapter and a separate invented fixture."""

from datetime import date, datetime, time, timedelta
from collections import defaultdict
from decimal import Decimal
import hashlib
from pathlib import Path

from app.backtest.options.domain import (
    CHICAGO, Contract, Coverage, Dataset, Quote, Rules, Signal, SignalArchive,
    TICKERS, Underlying, schedule, session_bounds,
)

ROOT = Path(__file__).resolve().parents[3]
# The existing desktop bundle includes static resources; this contains only
# frozen derived actions and their provenance, never the licensed quote tape.
ARCHIVE = ROOT / "static" / "archived-options-signals.json"
MAX_DATA_BYTES = 128 * 1024 * 1024


def read_archive(path: Path = ARCHIVE) -> SignalArchive:
    return SignalArchive.model_validate_json(path.read_bytes())


def load_dataset(path: Path, rules: Rules) -> tuple[Dataset, str]:
    with path.open("rb") as source:
        raw = source.read(MAX_DATA_BYTES + 1)
    if len(raw) > MAX_DATA_BYTES:
        raise ValueError("use targeted quote windows; the import exceeds 128 MiB")
    dataset = Dataset.model_validate_json(raw)
    if dataset.preset != rules.preset or dataset.source != "historical":
        raise ValueError("the import must be historical and match the selected preset")
    return dataset, hashlib.sha256(raw).hexdigest()


def batches(archive: SignalArchive, rules: Rules, start: date, end: date) -> list:
    grouped = {}
    for signal in archive.signals:
        if start <= signal.day <= end:
            grouped.setdefault(signal.day, []).append(signal)
    return [
        (day, *schedule(day, max(s.available_at for s in signals), rules), signals)
        for day, signals in sorted(grouped.items())
    ]


def coverage_for(dataset: Dataset | None, plan: list) -> dict:
    prices, entry_quotes = defaultdict(list), defaultdict(list)
    if dataset:
        tickers = {c.id: c.ticker for c in dataset.contracts}
        for row in dataset.underlying:
            prices[row.ticker].append(row)
        for row in dataset.quotes:
            entry_quotes[tickers[row.contract_id]].append(row.available_at)
    supported, unsupported = [], []
    for day, entry, exit_at, signals in plan:
        gaps = {}
        for ticker in TICKERS:
            status = dataset.coverage.get(day, {}).get(ticker) if dataset else None
            if status is None:
                gaps[ticker] = ["no historical coverage record"]
            elif not status.ready:
                gaps[ticker] = [key for key in ("discovery", "entry_quotes", "exit_quotes", "underlying", "adjustments")
                                if not getattr(status, key)] + ([status.note] if status.note else [])
            elif not any(entry - timedelta(minutes=2) <= row.observed_at <= row.available_at <= entry for row in prices[ticker]):
                gaps[ticker] = ["missing compatible underlying observation"]
            elif not any(entry - timedelta(minutes=2) <= stamp <= entry + timedelta(minutes=3) for stamp in entry_quotes[ticker]):
                gaps[ticker] = ["no historical option observations in entry window"]
        missing_signals = sorted(set(TICKERS) - {signal.ticker for signal in signals})
        if missing_signals:
            gaps["signals"] = missing_signals
        if dataset and exit_at > dataset.covered_through:
            gaps["archive"] = ["scheduled exit lies beyond the data cutoff"]
        row = {"signal_date": day, "entry_target": entry, "exit_target": exit_at}
        if gaps:
            unsupported.append({**row, "gaps": gaps})
        else:
            supported.append(row)
    return {"supported": supported, "unsupported": unsupported, "scheduled_dates": len(plan)}


def synthetic(rules: Rules, archive: SignalArchive) -> tuple[Dataset, SignalArchive]:
    """Use the feasibility sample dates with invented actions and quotes."""
    days = sorted({s.day for s in archive.signals})
    sample = (days[0], days[len(days)//2], days[-1])
    signals, contracts, prices, quotes, coverage = [], [], [], [], {}
    for day in sample:
        cutoff = datetime.combine(day, time(23, 59), CHICAGO)
        entry, exit_at = schedule(day, cutoff, rules)
        coverage[day] = {}
        for i, ticker in enumerate(TICKERS):
            signals.append(Signal(day=day, ticker=ticker, fast="BUY" if i < 4 else "HOLD",
                                  momentum="BUY" if i % 2 == 0 else "SELL", input_cutoff=cutoff, available_at=cutoff))
            expires = entry.date() + timedelta(days=rules.target_dte)
            while session_bounds(datetime.combine(expires, time(12), CHICAGO)) is None:
                expires += timedelta(days=1)
            contract = Contract(id=f"TEST-{rules.preset}-{day}-{ticker}", ticker=ticker, right="call",
                                strike=100, expiration=expires, listed_at=entry - timedelta(days=20),
                                last_trade_at=session_bounds(datetime.combine(expires, time(12), CHICAGO))[1],
                                multiplier=100, deliverable="standard_100_shares")
            contracts.append(contract)
            prices.append(Underlying(ticker=ticker, price=100, observed_at=entry, available_at=entry))
            entry_ask = Decimal("1.10") + Decimal(i) * Decimal("0.05")
            for stamp, ask in ((entry, entry_ask), (entry + timedelta(seconds=2), entry_ask),
                               (exit_at + timedelta(seconds=1), entry_ask + Decimal("0.12") * (2 - i))):
                quotes.append(Quote(contract_id=contract.id, observed_at=stamp, available_at=stamp,
                                    bid=max(Decimal("0.01"), ask - Decimal("0.05")), ask=ask, bid_size=10, ask_size=10))
            coverage[day][ticker] = Coverage(discovery=True, entry_quotes=True, exit_quotes=True, underlying=True, adjustments=True)
    return (
        Dataset(schema_version=1, preset=rules.preset, source="synthetic", resolution="tick",
                provider="Invented fixture", entitlement="Local test data", covered_through=max(q.available_at for q in quotes),
                coverage=coverage, contracts=contracts, underlying=prices, quotes=quotes),
        SignalArchive(source="synthetic", model="None", candidate="Invented BUY/HOLD/SELL fixtures",
                      provenance={"note": "Mechanics only; prices and signals are invented"}, signals=signals),
    )
