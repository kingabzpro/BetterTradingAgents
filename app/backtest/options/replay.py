"""Causal long-call selection and a paired, cash-constrained event replay."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from itertools import groupby
from statistics import mean, median
from threading import Event

from app.backtest.options.data import coverage_for
from app.backtest.options.domain import (
    CHICAGO, EXECUTION_POLICY_VERSION, PRESETS, Contract, Dataset, Execution, Quote, Rules, Signal, Underlying,
    schedule, session_bounds,
)

ARMS = ("fast", "momentum")
CENT = Decimal("0.01")
TICK_ENTRY_DELAY = timedelta(seconds=3)
MINUTE_ENTRY_DELAY = timedelta(minutes=3)
EXIT_WINDOW = timedelta(minutes=2)


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Pick:
    contract: Contract
    quote: Quote
    underlying: Underlying
    relative_spread: Decimal
    strike_distance: Decimal
    dte: int


class Tape:
    def __init__(self, dataset: Dataset):
        self.dataset = dataset
        self.quotes = defaultdict(list)
        self.contracts = defaultdict(list)
        self.contract_ids = {row.id: row for row in dataset.contracts}
        self.underlying = defaultdict(list)
        self.adjustments = defaultdict(list)
        for row in dataset.quotes:
            self.quotes[row.contract_id].append(row)
        for rows in self.quotes.values():
            rows.sort(key=lambda row: (row.available_at, row.chronological_key))
        for row in dataset.contracts:
            self.contracts[row.ticker].append(row)
        for row in dataset.underlying:
            self.underlying[row.ticker].append(row)
        for row in dataset.adjustments:
            self.adjustments[row.contract_id].append(row)

    @property
    def freshness(self):
        return timedelta(seconds=2 if self.dataset.resolution == "tick" else 120)

    def at(self, contract_id: str, at: datetime) -> Quote | None:
        bounds = session_bounds(at)
        contract = self.contract_ids.get(contract_id)
        if contract is None or not bounds:
            return None
        return max((q for q in self.quotes[contract_id] if q.available_at <= at
                    and q.observed_at >= at - self.freshness and self.in_session(q, contract, bounds)),
                   key=lambda q: q.chronological_key, default=None)

    @staticmethod
    def in_session(q: Quote, contract: Contract, bounds: tuple[datetime, datetime]) -> bool:
        start = q.bar_start or q.observed_at
        return (bounds[0] <= start <= q.observed_at <= q.available_at < min(bounds[1], contract.last_trade_at)
                and contract.listed_at <= q.observed_at)

    def pick(self, ticker: str, at: datetime, exit_at: datetime, rules: Rules) -> tuple[Pick | None, str]:
        spot = max((u for u in self.underlying[ticker] if u.available_at <= at
                    and at - timedelta(minutes=2) <= u.observed_at <= u.available_at),
                   key=lambda u: u.chronological_key, default=None)
        if spot is None:
            return None, "missing_or_stale_underlying"
        choices = []
        for c in self.contracts[ticker]:
            dte = (c.expiration - at.astimezone(CHICAGO).date()).days
            if (c.right != "call" or c.multiplier != 100 or c.deliverable != "standard_100_shares"
                or not c.listed_at <= at < exit_at < c.last_trade_at or exit_at.astimezone(CHICAGO).date() >= c.expiration
                or not rules.min_dte <= dte <= rules.max_dte):
                continue
            if any(a.effective_at <= at for a in self.adjustments[c.id]):
                continue
            distance = abs(c.strike - spot.price) / spot.price
            if distance > Decimal("0.02"):
                continue
            quote = self.at(c.id, at)
            if quote is None or not quote.valid:
                continue
            absolute = quote.ask - quote.bid
            relative = absolute / ((quote.ask + quote.bid) / 2)
            if relative <= Decimal("0.10") and absolute <= Decimal("0.20"):
                choices.append(Pick(c, quote, spot, relative, distance, dte))
        if not choices:
            return None, "no_eligible_entry_contract"
        return min(choices, key=lambda p: (abs(p.dte - rules.target_dte), p.strike_distance, p.relative_spread,
                                           p.contract.expiration, p.contract.strike, p.contract.id)), ""

    def entry_quote(self, c: Contract, selection: datetime, order: datetime, fill: datetime) -> tuple[Quote | None, str | None]:
        bounds = session_bounds(fill)
        if not bounds or not bounds[0] <= order <= fill < bounds[1]:
            return None, "entry_outside_regular_session"
        adjustment = self.next_adjustment(c.id, selection)
        if adjustment and adjustment <= fill:
            return None, "contract_adjusted_before_fill"
        q = self.at(c.id, fill)
        if q is None:
            return None, "missing_or_stale_entry_quote"
        if not q.valid:
            return q, "invalid_two_sided_entry_quote"
        if (q.bar_start or q.observed_at) < order:
            return q, "entry_quote_precedes_order"
        if q.ask_size == 0 or (q.ask_size is None and self.dataset.resolution == "tick"):
            return q, "insufficient_displayed_ask_size"
        return q, None

    def exit_quote(self, c: Contract, order: datetime, limit: datetime, slip: Decimal) -> Quote | None:
        adjustment = self.next_adjustment(c.id, order)
        latest = None
        for available, quotes in groupby(self.quotes[c.id], key=lambda q: q.available_at):
            if available > limit:
                break
            if available >= c.last_trade_at or (adjustment and available >= adjustment):
                break
            bounds = session_bounds(available) if available >= order else None
            if not bounds:
                continue
            q = max((quote for quote in quotes if self.in_session(quote, c, bounds)),
                    key=lambda quote: quote.chronological_key, default=None)
            if q is None or (latest is not None and q.chronological_key <= latest):
                continue
            latest = q.chronological_key
            if (q.bar_start or q.observed_at) < order or not q.valid or q.available_at - q.observed_at > self.freshness:
                continue
            if q.bid_size == 0 or (q.bid_size is None and self.dataset.resolution == "tick"):
                continue
            if q.bid <= slip:
                continue
            return q
        return None

    def next_adjustment(self, contract_id: str, from_at: datetime) -> datetime | None:
        return min((row.effective_at for row in self.adjustments[contract_id] if row.effective_at >= from_at), default=None)


@dataclass
class Position:
    contract: Contract
    trade: dict
    exit_order: datetime
    exit_quote: Quote | None
    adjustment_at: datetime | None


@dataclass
class Account:
    cash: Decimal
    execution: Execution
    slip: Decimal
    open: list[Position] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    research: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    peak_commitment: Decimal = Decimal(0)
    peak_exposure_pct: Decimal = Decimal(0)
    unpaid_research: Decimal = Decimal(0)
    processed_signal_dates: set[date] = field(default_factory=set)

    @property
    def fee(self):
        return self.execution.commission + self.execution.other_fee_per_side

    @property
    def available_cash(self):
        return self.cash - self.fee * len(self.open)

    def settle(self, at: datetime, intraday: bool) -> None:
        for p in self.open[:]:
            q = p.exit_quote
            if q is None or q.available_at > at:
                continue
            premium = q.bid - self.slip
            proceeds = money(premium * p.contract.multiplier)
            cash_before = self.cash
            self.cash += proceeds - self.fee
            self.fees += self.fee
            p.trade.update(exit_at=q.available_at, exit_quote=q, exit_premium=premium, proceeds=proceeds,
                           exit_order_at=p.exit_order, exit_size_unknown=q.bid_size is None,
                           exit_cash_before=cash_before, exit_cash_after=self.cash, exit_fee=self.fee,
                           realized_pnl=proceeds - p.trade["premium_committed"] - 2 * self.fee,
                           same_day_breach=intraday and q.available_at.astimezone(CHICAGO).date() != p.trade["entry_at"].astimezone(CHICAGO).date(),
                           delayed_exit=q.available_at > p.exit_order + EXIT_WINDOW)
            self.open.remove(p)

    def charge(self, amount: Decimal) -> bool:
        self.research += amount
        paid = min(amount, self.available_cash)
        self.cash -= paid
        self.unpaid_research += amount - paid
        return self.unpaid_research == 0

    def enter_batch(self, tape: Tape, rules: Rules, signals: list[Signal], arm: str, day: date,
                    entry: datetime, exit_at: datetime, blocked: str) -> None:
        if day in self.processed_signal_dates:
            return
        self.processed_signal_dates.add(day)
        research_charge = sum((s.research_cost for s in signals), Decimal(0)) if arm == "fast" else Decimal(0)
        if not self.charge(research_charge):
            blocked = "research_cost_exceeds_available_cash"
        if self.open and not blocked:
            blocked = "prior_position_still_open"
        order = entry + timedelta(seconds=1)
        fill_at = entry + (TICK_ENTRY_DELAY if tape.dataset.resolution == "tick" else MINUTE_ENTRY_DELAY)
        planned = []
        for s in signals:
            action = getattr(s, arm)
            row = {"signal": s, "action": action, "signal_date": day, "ticker": s.ticker, "selection_at": entry}
            self.decisions.append(row)
            if action == "BUY":
                row["intent_id"] = f"{day}:{s.ticker}:buy"
            if action != "BUY" or blocked:
                row["status"] = blocked if action == "BUY" else action.lower()
                continue
            pick, reason = tape.pick(s.ticker, entry, exit_at, rules)
            if pick is None:
                row["status"] = reason
                continue
            row.update(contract=pick.contract, selection_quote=pick.quote, underlying=pick.underlying, dte=pick.dte,
                       relative_spread=pick.relative_spread, strike_distance=pick.strike_distance, order_at=order,
                       fill_target_at=fill_at)
            planned.append((pick, row))
        budget = min(self.available_cash, self.cash * self.execution.premium_budget)
        committed = Decimal(0)
        for pick, row in sorted(planned, key=lambda item: (item[0].relative_spread, item[0].strike_distance, item[0].contract.ticker)):
            c = pick.contract
            row.update(batch_budget=budget, batch_committed_before=committed, entry_fee=self.fee, exit_fee_reserve=self.fee)
            q, reason = tape.entry_quote(c, entry, order, fill_at)
            if reason:
                row.update(status="missing_or_unexecutable_entry_quote", entry_evidence_reason=reason, rejected_entry_quote=q)
                continue
            assert q is not None
            premium = q.ask + self.slip
            cost = money(premium * c.multiplier)
            needed = cost + 2 * self.fee
            row.update(modeled_entry_quote=q, modeled_premium=premium, modeled_commitment=needed,
                       affordable=needed <= budget)
            if needed > budget - committed:
                row["status"] = "unaffordable" if needed > budget else "shared_premium_budget_exhausted"
                continue
            committed += needed
            cash_before = self.cash
            self.cash -= cost + self.fee
            self.fees += self.fee
            adjustment = tape.next_adjustment(c.id, fill_at)
            limit = min(tape.dataset.covered_through, c.last_trade_at)
            if adjustment:
                limit = min(limit, adjustment - timedelta(microseconds=1))
            sale = tape.exit_quote(c, exit_at, limit, self.slip)
            trade = dict(signal_date=day, ticker=c.ticker, contract_id=c.id, multiplier=c.multiplier, contracts=1,
                         intent_id=row["intent_id"], entry_cash_before=cash_before, entry_cash_after=self.cash,
                         entry_fee=self.fee, exit_fee_reserve=self.fee,
                         selection_at=entry, entry_order_at=order, entry_at=fill_at, entry_quote=q,
                         entry_premium=premium, premium_committed=cost, fee_per_side=self.fee,
                         scheduled_exit=exit_at, expiration=c.expiration, entry_size_unknown=q.ask_size is None)
            self.trades.append(trade)
            self.open.append(Position(c, trade, exit_at, sale, adjustment))
            row["status"] = "filled"
        self.peak_commitment = max(self.peak_commitment, committed)
        if budget and committed:
            opening_equity = self.cash + sum(p.trade["premium_committed"] + self.fee for p in self.open)
            self.peak_exposure_pct = max(self.peak_exposure_pct, 100 * committed / opening_equity)

    def mark(self, tape: Tape, at: datetime) -> dict:
        values = []
        for p in self.open:
            q = tape.at(p.contract.id, at)
            if (q is None or not q.valid or at >= p.contract.last_trade_at
                or (p.adjustment_at and at >= p.adjustment_at)):
                values.append(None)
            else:
                values.append(money(q.bid * p.contract.multiplier))
        full_mark = None if any(v is None for v in values) else sum(values, Decimal(0))
        return {"cash": self.cash, "available_cash": self.available_cash,
                "open_contracts": len(self.open), "cost_basis": sum(p.trade["premium_committed"] for p in self.open),
                "marked_equity": self.cash + full_mark - self.unpaid_research if full_mark is not None else None,
                "unrealized_pnl": full_mark - sum(p.trade["premium_committed"] + self.fee for p in self.open)
                if full_mark is not None else None}

    def summary(self, tape: Tape, intraday: bool, mark_rows: list, arm: str) -> dict:
        intraday_breach = 0
        for p in self.open:
            breached = intraday and tape.dataset.covered_through >= session_bounds(p.exit_order)[1]
            p.trade.update(unresolved=True, same_day_breach=breached,
                           exit_order_at=p.exit_order, scheduled_exit_missed=True,
                           unresolved_reason="corporate_action" if p.adjustment_at and p.adjustment_at <= tape.dataset.covered_through
                           else "expiration_outcome_unverified" if p.contract.last_trade_at <= tape.dataset.covered_through
                           else "missing_or_unexecutable_exit_quote")
            intraday_breach += int(breached)
        realized = [t["realized_pnl"] for t in self.trades if "realized_pnl" in t]
        end = self.cash - self.unpaid_research if not self.open else None
        start = self.execution.starting_cash
        skips = Counter((r["ticker"], r["status"]) for r in self.decisions if r["action"] == "BUY" and r["status"] != "filled")
        marks = [start, *(r[arm]["marked_equity"] for r in mark_rows)]
        drawdown = None
        if all(x is not None for x in marks):
            peak = start
            drawdown = Decimal(0)
            for value in marks:
                peak = max(peak, value)
                drawdown = max(drawdown, 100 * (peak - value) / peak)
        return {"starting_equity": start, "ending_equity": end, "cash": self.cash, "available_cash": self.available_cash,
                "open_positions": self.mark(tape, tape.dataset.covered_through),
                "net_return_pct": (end / start - 1) * 100 if end is not None else None,
                "trading_only_ending_equity_same_fills": end + self.research if end is not None else None,
                "trading_only_return_pct_same_fills": ((end + self.research) / start - 1) * 100 if end is not None else None,
                "realized_pnl": sum(realized, Decimal(0)), "fees": self.fees, "commission": self.fees - self.execution.other_fee_per_side * (len(self.trades) + len(realized)),
                "other_fees": self.execution.other_fee_per_side * (len(self.trades) + len(realized)), "research_costs": self.research,
                "unpaid_research": self.unpaid_research, "buy_signals": sum(r["action"] == "BUY" for r in self.decisions),
                "eligible_contracts": sum("contract" in r for r in self.decisions), "affordable_candidates": sum(r.get("affordable", False) for r in self.decisions),
                "positions_opened": len(self.trades), "closed_trades": len(realized), "unresolved_positions": len(self.open),
                "intraday_breaches": intraday_breach + sum(t.get("same_day_breach", False) for t in self.trades if not t.get("unresolved")),
                "premium_committed_total": sum(t["premium_committed"] for t in self.trades),
                "peak_batch_commitment_with_fees": self.peak_commitment, "peak_batch_exposure_pct": self.peak_exposure_pct,
                "average_trade_pnl": mean(realized) if realized else None, "median_trade_pnl": median(realized) if realized else None,
                "cohort_end_drawdown_pct": drawdown, "executability": "insufficiently executable" if len(self.trades) < 3 else "executed at least three positions",
                "skips": [{"ticker": ticker, "reason": reason, "count": count} for (ticker, reason), count in sorted(skips.items())],
                "trades": self.trades, "decisions": self.decisions}


class Cancelled(Exception):
    pass


def _paired_comparison(plan: list, marks: list, missing_marks: dict, starting_cash: Decimal) -> tuple[list, dict]:
    marks_by_day = {row["signal_date"]: row for row in marks}
    previous = dict.fromkeys(ARMS, starting_cash)
    rows = []
    reasons = Counter()
    for day, _, exit_at, _ in sorted(plan, key=lambda cohort: (cohort[2], cohort[0])):
        mark = marks_by_day.get(day)
        gap = missing_marks.get(day)
        values = {arm: mark[arm]["marked_equity"] if mark else None for arm in ARMS}
        row_reasons = [gap] if gap else []
        returns = dict.fromkeys(ARMS)
        if not gap:
            for arm in ARMS:
                if values[arm] is None:
                    row_reasons.append(f"{arm}_scheduled_mark_unavailable")
                elif previous[arm] is None:
                    row_reasons.append(f"{arm}_previous_scheduled_mark_unavailable")
                elif not previous[arm]:
                    row_reasons.append(f"{arm}_previous_equity_zero")
                else:
                    returns[arm] = 100 * (values[arm] / previous[arm] - 1)
        difference = returns["fast"] - returns["momentum"] if not row_reasons else None
        reasons.update(row_reasons)
        rows.append({"signal_date": day, "at": exit_at + EXIT_WINDOW, **returns,
                     "difference_pp": difference, "missing_reasons": row_reasons})
        previous = values
    usable = sum(row["difference_pp"] is not None for row in rows)
    return rows, {"status": "unavailable" if not usable else "complete" if usable == len(rows) else "partial",
                  "scheduled": len(rows), "usable": usable, "missing": len(rows) - usable,
                  "missing_reasons": dict(sorted(reasons.items()))}


def simulate(dataset: Dataset, plan: list, rules: Rules, execution: Execution, slip: Decimal,
             cancel: Event, progress, coverage: dict) -> dict:
    tape = Tape(dataset)
    accounts = {arm: Account(execution.starting_cash, execution, slip) for arm in ARMS}
    supported = {row["signal_date"] for row in coverage["supported"]}
    events = [(entry, 1, i) for i, (_, entry, _, _) in enumerate(plan)]
    events += [(exit_at + EXIT_WINDOW, 0, i)
               for i, (_, _, exit_at, _) in enumerate(plan) if dataset.covered_through >= exit_at + EXIT_WINDOW]
    marks, accepted = [], set()
    missing_marks = {}
    completed = 0
    previous_exit = None
    intraday = rules.holding_sessions == 1
    for stamp, kind, i in sorted(events):
        if cancel.is_set():
            raise Cancelled
        day, entry, exit_at, signals = plan[i]
        for account in accounts.values():
            account.settle(stamp, intraday)
        if kind == 0:
            if day in accepted:
                marks.append({"signal_date": day, "at": stamp, **{arm: a.mark(tape, stamp) for arm, a in accounts.items()}})
            continue
        blocked = "unsupported_historical_date" if day not in supported else ""
        if not blocked and previous_exit is not None and entry <= previous_exit:
            blocked = "scheduled_cohort_overlap"
        if not blocked:
            previous_exit = exit_at
            accepted.add(day)
            if exit_at + EXIT_WINDOW > dataset.covered_through:
                missing_marks[day] = "comparison_window_not_covered"
        else:
            missing_marks[day] = blocked
        for arm, account in accounts.items():
            account.enter_batch(tape, rules, signals, arm, day, entry, exit_at, blocked)
        completed += 1
        progress(completed, len(plan))
    for account in accounts.values():
        account.settle(dataset.covered_through, intraday)
    paired, comparison = _paired_comparison(plan, marks, missing_marks, execution.starting_cash)
    summaries = {arm: a.summary(tape, intraday, marks, arm) for arm, a in accounts.items()}
    execution_complete = all(not (row["unresolved_positions"] or row["unpaid_research"]) for row in summaries.values())
    data_complete = not (coverage["unsupported"] or any(reason == "comparison_window_not_covered" for reason in missing_marks.values())
                         or any(row[arm]["marked_equity"] is None for row in marks for arm in ARMS))
    return {"slippage_per_unit": slip, "accounts": summaries, "balances_as_of": dataset.covered_through,
            "execution_status": "complete" if execution_complete else "incomplete",
            "data_status": "complete" if data_complete else "incomplete", "comparison": comparison,
            "cohort_end_marks": marks, "paired_differences": paired}


def replay(dataset: Dataset, plan: list, rules: Rules, execution: Execution, cancel: Event, progress) -> dict:
    if rules != PRESETS[dataset.preset]:
        raise ValueError("replay requires the frozen rules for the data preset")
    execution = Execution.model_validate(execution.model_dump())
    seen = set()
    for day, entry, exit_at, signals in plan:
        if day in seen:
            raise ValueError("duplicate signal date in replay plan")
        if not signals or any(s.day != day for s in signals) or len({s.ticker for s in signals}) != len(signals):
            raise ValueError("replay requires distinct ticker signals for each signal date")
        if (entry, exit_at) != schedule(day, max(s.available_at for s in signals), rules):
            raise ValueError("replay requires the scheduled entry and exit for the preset")
        seen.add(day)
    coverage = coverage_for(dataset, plan)
    if not coverage["supported"]:
        raise ValueError("no scheduled dates have the required market-data coverage")
    primary = simulate(dataset, plan, rules, execution, execution.slippage, cancel,
                       lambda n, total: progress(n, 2 * total), coverage)
    stress = simulate(dataset, plan, rules, execution, execution.stress_slippage, cancel,
                      lambda n, total: progress(total + n, 2 * total), coverage)
    incomplete = any("complete" != scenario[key] for scenario in (primary, stress) for key in ("execution_status", "data_status")) or any(
        scenario["comparison"]["status"] != "complete" for scenario in (primary, stress))
    return {"coverage": coverage, "incomplete": incomplete, "primary": primary, "stress": stress,
            "account_mode": "simulated", "execution_policy_version": EXECUTION_POLICY_VERSION,
            "limitations": ["Exploratory research assumptions; modeled fills and charges.",
                            "Trading-only figures add back research costs on the same executed ledger.",
                            "Cohort-end marks are the only drawdown resolution. Final balances can reflect exits after scheduled marks."]}
