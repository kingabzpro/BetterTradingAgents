"""Check prospective entry sealing and replay reproduction with an offline fixture."""

from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from threading import Event
from unittest.mock import patch

from app.backtest.options.data import coverage_for
from app.backtest.options.domain import CHICAGO, PRESETS, TICKERS, Contract, Coverage, Dataset, Execution, Quote, Signal, Underlying, schedule
from app.backtest.options.replay import EXIT_WINDOW, TICK_ENTRY_DELAY, Tape, replay, seal_entry


RULES = PRESETS["swing_5_sessions"]
EXECUTION = Execution()


def fixture():
    day = date(2026, 11, 24)
    cutoff = datetime.combine(day - timedelta(days=1), time(15), CHICAGO)
    freeze = datetime.combine(day, time(8, 35), CHICAGO)
    entry, exit_at = schedule(day, freeze, RULES)
    expiration = day + timedelta(days=17)
    rows = []
    contracts = []
    underlying = []
    quotes = []
    covered = {}
    for i, ticker in enumerate(TICKERS):
        rows.append(Signal(day=day, ticker=ticker, fast="BUY" if i < 4 else "HOLD",
                           momentum="BUY" if i in (0, 5) else "SELL", input_cutoff=cutoff,
                           generated_at=freeze - timedelta(minutes=1), available_at=freeze, research_cost="0.12"))
        contract = Contract(id=f"CONTRACT-{ticker}", ticker=ticker, right="call", strike="100",
                            expiration=expiration, listed_at=cutoff - timedelta(days=7),
                            last_trade_at=datetime.combine(expiration, time(15), CHICAGO), multiplier=100,
                            deliverable="standard_100_shares")
        contracts.append(contract)
        spot_at = entry - timedelta(seconds=10)
        underlying.append(Underlying(ticker=ticker, price="100", observed_at=spot_at, available_at=spot_at))
        for seconds in (-1, 2):
            observed = entry + timedelta(seconds=seconds)
            quotes.append(Quote(contract_id=contract.id, bid="1.14", ask="1.20", bid_size=5, ask_size=5,
                                observed_at=observed, available_at=observed + timedelta(milliseconds=500)))
        covered[ticker] = Coverage(discovery=True, entry_quotes=True, exit_quotes=False, underlying=True, adjustments=True)
    dataset = Dataset(schema_version=1, preset=RULES.preset, source="prospective", resolution="snapshot",
                      provider="Offline snapshot fixture", entitlement="Offline check",
                      covered_through=entry + TICK_ENTRY_DELAY, coverage={day: covered},
                      contracts=contracts, underlying=underlying, quotes=quotes)
    return dataset, (day, entry, exit_at, rows)


def replacement(dataset, **changes):
    return Dataset.model_validate({**dataset.model_dump(), **changes})


def check_entry(dataset, cohort):
    day, entry, exit_at, _ = cohort
    assert entry.strftime("%Y-%m-%d %H:%M") == "2026-11-24 08:40"
    assert exit_at.strftime("%Y-%m-%d %H:%M") == "2026-12-01 14:45"
    assert coverage_for(dataset, [cohort], entry_only=True)["supported"]
    assert not coverage_for(dataset, [cohort])["supported"]
    with patch.object(Tape, "exit_quote", side_effect=AssertionError("entry cannot consult exits")):
        receipt = seal_entry(dataset, cohort, RULES, EXECUTION)
    assert receipt["entry_at"] == entry.isoformat() and receipt["resolution"] == "snapshot"
    for scenario in ("primary", "stress"):
        accounts = receipt["accounts"][scenario]
        assert Decimal(accounts["fast"]["research_costs"]) == Decimal("0.72")
        assert Decimal(accounts["momentum"]["research_costs"]) == 0
        assert len(accounts["fast"]["trades"]) == 3 and len(accounts["momentum"]["trades"]) == 2
        assert any(row["status"] == "shared_premium_budget_exhausted" for row in accounts["fast"]["decisions"])
        assert all("exit_at" not in row for row in accounts["fast"]["trades"])
        assert all(row["selection_at"] == entry.isoformat() for row in accounts["fast"]["trades"])
        assert all(row["entry_at"] == (entry + TICK_ENTRY_DELAY).isoformat() for row in accounts["fast"]["trades"])
    assert receipt["accounts"]["primary"]["fast"]["cash"] == "2134.33"
    assert receipt["accounts"]["primary"]["fast"]["available_cash"] == "2132.38"
    return receipt


def assert_rejected(call, reason):
    try:
        call()
    except ValueError as error:
        assert reason in str(error), error
    else:
        raise AssertionError(f"expected rejection: {reason}")


def run(dataset, cohort, receipt):
    return replay(dataset, [cohort], RULES, EXECUTION, Event(), lambda *_: None, sealed_entry=receipt)


def check_timing(dataset, cohort, receipt):
    day, entry, exit_at, rows = cohort
    assert_rejected(lambda: run(dataset, cohort, receipt), "awaiting scheduled exit evidence")

    # A fresh stock or option observation received after the fill cannot change the sealed entry.
    late = entry + timedelta(seconds=4)
    quote = dataset.quotes[0].model_copy(update={"bid": Decimal("1.00"), "ask": Decimal("1.01"), "available_at": late})
    spot = dataset.underlying[0].model_copy(update={"available_at": late, "price": Decimal(150)})
    later = replacement(dataset, covered_through=late, quotes=[*dataset.quotes, quote],
                        underlying=[*dataset.underlying, spot])
    assert seal_entry(later, cohort, RULES, EXECUTION) == receipt

    late_signal = rows[0].model_copy(update={"available_at": entry + timedelta(seconds=1)})
    schedule_again = schedule(day, late_signal.available_at, RULES)
    assert_rejected(lambda: seal_entry(dataset, (day, *schedule_again, [late_signal, *rows[1:]]), RULES, EXECUTION),
                    "must freeze by 08:35")

    # Capture gaps at the modeled fill cannot be mistaken for cash outcomes.
    stale_at_fill = [q for q in dataset.quotes if q.observed_at <= entry]
    assert_rejected(lambda: seal_entry(replacement(dataset, quotes=stale_at_fill), cohort, RULES, EXECUTION),
                    "missing fill-time quote evidence")
    too_early = entry + timedelta(milliseconds=900)
    early_quotes = [q.model_copy(update={"observed_at": too_early, "available_at": too_early})
                    if q.observed_at > entry else q for q in dataset.quotes]
    assert_rejected(lambda: seal_entry(replacement(dataset, quotes=early_quotes), cohort, RULES, EXECUTION),
                    "missing fill-time quote evidence")
    without_size = [q.model_copy(update={"ask_size": None}) if q.observed_at > entry else q for q in dataset.quotes]
    assert_rejected(lambda: seal_entry(replacement(dataset, quotes=without_size), cohort, RULES, EXECUTION),
                    "missing fill-time quote evidence")

    # A timely explicit refusal is a known cash outcome.
    zero_size = [q.model_copy(update={"ask_size": 0}) if q.observed_at > entry else q for q in dataset.quotes]
    unexecutable = seal_entry(replacement(dataset, quotes=zero_size), cohort, RULES, EXECUTION)
    assert not unexecutable["accounts"]["primary"]["fast"]["trades"]
    assert any(r.get("entry_evidence_reason") == "insufficient_displayed_ask_size"
               for r in unexecutable["accounts"]["primary"]["fast"]["decisions"])
    crossed = [q.model_copy(update={"bid": Decimal("2")}) if q.observed_at > entry else q for q in dataset.quotes]
    invalid = seal_entry(replacement(dataset, quotes=crossed), cohort, RULES, EXECUTION)
    assert not invalid["accounts"]["primary"]["fast"]["trades"]
    assert any(r.get("entry_evidence_reason") == "invalid_two_sided_entry_quote"
               for r in invalid["accounts"]["primary"]["fast"]["decisions"])

    # Verified empty candidate universe is distinguishable from missing discovery/quotes.
    empty_chain = replacement(dataset, contracts=[], quotes=[])
    assert not seal_entry(empty_chain, cohort, RULES, EXECUTION)["accounts"]["primary"]["fast"]["trades"]
    incomplete = replacement(empty_chain, coverage={day: {
        ticker: row.model_copy(update={"entry_quotes": False}) for ticker, row in dataset.coverage[day].items()
    }})
    assert_rejected(lambda: seal_entry(incomplete, cohort, RULES, EXECUTION), "entry evidence is incomplete")


def check_reproduction(dataset, cohort, receipt):
    day, entry, exit_at, _ = cohort
    proved = {day: {ticker: flags.model_copy(update={"exit_quotes": True})
                    for ticker, flags in dataset.coverage[day].items()}}
    exit_time = exit_at + timedelta(seconds=1)
    exits = [Quote(contract_id=c.id, bid="1.30", ask="1.40", bid_size=5, ask_size=5,
                   observed_at=exit_time, available_at=exit_time) for c in dataset.contracts]
    finished = replacement(dataset, covered_through=exit_at + EXIT_WINDOW, coverage=proved, quotes=[*dataset.quotes, *exits])
    paired = run(finished, cohort, receipt)
    assert paired["sealed_entry_reproduced"] and not paired["incomplete"]
    assert paired["primary"]["comparison"]["usable"] == 1
    assert paired["primary"]["accounts"]["fast"]["closed_trades"] == 3
    assert paired["primary"]["accounts"]["momentum"]["closed_trades"] == 2

    touched = deepcopy(receipt)
    touched["accounts"]["primary"]["fast"]["cash"] = "2500.00"
    assert_rejected(lambda: run(finished, cohort, touched), "does not reproduce")
    priced = [q.model_copy(update={"ask": Decimal("1.24")}) if q.observed_at == entry+timedelta(seconds=2) else q
              for q in finished.quotes]
    assert_rejected(lambda: run(replacement(finished, quotes=priced), cohort, receipt), "does not reproduce")

    # Attempted but missing exits close neither positions nor the matched comparison.
    attempted = replacement(dataset, covered_through=exit_at+EXIT_WINDOW)
    incomplete = run(attempted, cohort, receipt)
    assert incomplete["incomplete"] and incomplete["primary"]["data_status"] == "incomplete"
    assert incomplete["primary"]["accounts"]["fast"]["unresolved_positions"] == 3
    assert incomplete["primary"]["accounts"]["fast"]["ending_equity"] is None
    assert incomplete["primary"]["comparison"]["usable"] == 0

    # Quotes arriving after the mark can close positions without repairing it.
    later = exit_at + timedelta(minutes=4)
    delayed = [q.model_copy(update={"observed_at": later, "available_at": later}) for q in exits]
    settled = run(replacement(finished, covered_through=later, quotes=[*dataset.quotes,*delayed]), cohort, receipt)
    assert settled["primary"]["execution_status"] == "complete"
    assert settled["primary"]["comparison"]["usable"] == 0
    assert settled["primary"]["accounts"]["fast"]["closed_trades"] == 3
    assert all(t["delayed_exit"] for t in settled["primary"]["accounts"]["fast"]["trades"])


if __name__ == "__main__":
    entry_dataset, scheduled_cohort = fixture()
    sealed = check_entry(entry_dataset, scheduled_cohort)
    check_timing(entry_dataset, scheduled_cohort, sealed)
    check_reproduction(entry_dataset, scheduled_cohort, sealed)
    print("Prospective five-session entry, deadline, and final replay verified (offline fixture)")
