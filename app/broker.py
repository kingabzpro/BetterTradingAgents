"""Alpaca paper-trading connection and order lifecycle (ROADMAP P2.1).

Paper only: the SDK client is constructed with paper=True hardcoded and
there is no base-url configuration, so nothing can point this feature at
the live broker. This module owns the Alpaca client, the local
broker_orders table (orders this app placed plus the decision linkage,
which Alpaca does not store), and the submission guard rails. Nothing
else in the app imports alpaca. The Alpaca account, positions, and equity
curve are the source of truth and are fetched fresh on every read.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum

import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import GetPortfolioHistoryRequest, MarketOrderRequest

from app.config import settings
from app.models import BrokerAccount, BrokerOrder, BrokerPosition, BrokerStatus

logger = logging.getLogger("broker")

PAPER_DOCS_URL = "https://docs.alpaca.markets/us/docs/paper-trading"

TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")

# Terminal for our purposes; anything else may still move or be canceled.
TERMINAL_ORDER_STATUSES = ("filled", "canceled", "expired", "rejected")

EQUITY_PERIODS = ("1W", "1M", "3M", "1A")


class BrokerNotConfigured(ValueError):
    """Keys are missing; the UI should show the setup hint."""


class BrokerRuleError(ValueError):
    """A submission-guard violation or upstream failure, with an HTTP status
    for main.py to pass through."""

    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.status_code = status_code


def _client() -> TradingClient:
    """The single construction point for the Alpaca client.

    Paper only: paper=True is hardcoded. Checks monkeypatch this function;
    no other module imports alpaca.
    """
    if not settings.alpaca_configured:
        raise BrokerNotConfigured(
            "Alpaca paper trading is not configured; add the API key ID and "
            "secret key on the Settings page"
        )
    return TradingClient(
        settings.alpaca_api_key_id,
        settings.alpaca_api_secret_key,
        paper=True,
    )


def _map_broker_error(exc: APIError) -> str:
    """Human message for an Alpaca error. Status and Alpaca's own message
    only; never request bodies or credentials."""
    if exc.status_code == 401:
        return "Alpaca rejected the credentials; check the ALPACA API keys"
    if exc.status_code == 429:
        return "Alpaca rate limit hit, try again shortly"
    if exc.status_code in (403, 422):
        try:
            return f"Alpaca rejected the request: {exc.message}"
        except Exception:  # noqa: BLE001 - body was not the expected JSON
            return f"Alpaca rejected the request (HTTP {exc.status_code})"
    return f"Alpaca API error (HTTP {exc.status_code})"


async def _call(fn):
    """Run one sync SDK call off the event loop, mapping SDK errors."""
    try:
        return await asyncio.to_thread(fn)
    except APIError as exc:
        raise BrokerRuleError(_map_broker_error(exc), status_code=503) from exc


def _f(value) -> float | None:
    """Alpaca ships money fields as strings; empty stays None, never 0."""
    return None if value in (None, "") else float(value)


def _text(value) -> str:
    return str(value.value if isinstance(value, Enum) else value or "")


async def status() -> BrokerStatus:
    """Config read only, no HTTP call: the page can render before any fetch."""
    return BrokerStatus(
        configured=settings.alpaca_configured,
        enabled=settings.alpaca_trading_enabled,
        paper_url=PAPER_DOCS_URL,
        max_order_usd=settings.alpaca_max_order_usd,
    )


async def clock() -> dict:
    """Market calendar: whether the paper market is open right now."""

    def _fetch():
        raw = _client().get_clock()
        return {
            "is_open": bool(raw.is_open),
            "next_open": str(getattr(raw, "next_open", "") or ""),
        }

    return await _call(_fetch)


async def account() -> BrokerAccount:
    """Fresh paper-account snapshot (also the Test connection action)."""

    def _fetch() -> BrokerAccount:
        raw = _client().get_account()
        return BrokerAccount(
            account_number=raw.account_number or "",
            status=_text(raw.status),
            equity=_f(raw.equity),
            cash=_f(raw.cash),
            buying_power=_f(raw.buying_power),
            last_equity=_f(raw.last_equity),
            trading_blocked=bool(raw.trading_blocked),
        )

    return await _call(_fetch)


async def positions() -> list[BrokerPosition]:
    """Fresh open paper positions."""

    def _fetch() -> list[BrokerPosition]:
        return [
            BrokerPosition(
                symbol=row.symbol,
                quantity=float(row.qty),
                avg_entry_price=float(row.avg_entry_price),
                current_price=_f(row.current_price),
                market_value=_f(row.market_value),
                unrealized_pl=_f(row.unrealized_pl),
                unrealized_plpc=_f(row.unrealized_plpc),
            )
            for row in _client().get_all_positions()
        ]

    return await _call(_fetch)


async def init() -> None:
    """Create the local order table. Reads stay available regardless of the
    kill switch; the table only ever holds orders this app placed."""

    def _ddl() -> None:
        with _connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL UNIQUE,
                    alpaca_order_id TEXT,
                    run_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                    notional REAL NOT NULL,
                    order_type TEXT NOT NULL DEFAULT 'market',
                    time_in_force TEXT NOT NULL DEFAULT 'day',
                    status TEXT NOT NULL,
                    decision TEXT,
                    confidence REAL,
                    decision_as_of TEXT,
                    filled_qty REAL,
                    filled_avg_price REAL,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_broker_orders_created "
                "ON broker_orders(created_at)"
            )

    await asyncio.to_thread(_ddl)
    if not settings.alpaca_configured:
        logger.info("[broker] paper trading not configured (dormant)")
    else:
        logger.info(
            "[broker] Alpaca paper connection configured | submissions=%s",
            "on" if settings.alpaca_trading_enabled else "off (kill switch)",
        )


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    return connection


# ---- local order rows ------------------------------------------------------


def _row_to_order(row: sqlite3.Row) -> BrokerOrder:
    return BrokerOrder(
        client_order_id=row["client_order_id"],
        alpaca_order_id=row["alpaca_order_id"],
        run_id=row["run_id"],
        ticker=row["ticker"],
        side=row["side"],
        notional=row["notional"],
        status=row["status"],
        decision=row["decision"],
        confidence=row["confidence"],
        decision_as_of=row["decision_as_of"],
        filled_qty=row["filled_qty"],
        filled_avg_price=row["filled_avg_price"],
        error=row["error"],
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


def _get_order_row(client_order_id: str) -> sqlite3.Row | None:
    with _connect() as connection:
        return connection.execute(
            "SELECT * FROM broker_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()


def _has_active_order(run_id: str, ticker: str, side: str) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM broker_orders WHERE run_id = ? AND ticker = ? AND side = ? "
            f"AND status NOT IN ({','.join('?' * len(TERMINAL_ORDER_STATUSES))})",
            (run_id, ticker, side, *TERMINAL_ORDER_STATUSES),
        ).fetchone()
    return row is not None


def _non_rejected_orders_today() -> int:
    with _connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM broker_orders "
            "WHERE status != 'rejected' AND created_at >= date('now')"
        ).fetchone()
    return int(row["n"])


def _insert_order(
    client_order_id: str,
    run_id: str,
    ticker: str,
    side: str,
    notional: float,
    decision: str,
    confidence: float,
    decision_as_of: str,
) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO broker_orders (
                client_order_id, run_id, ticker, side, notional,
                status, decision, confidence, decision_as_of
            ) VALUES (?, ?, ?, ?, ?, 'pending_submit', ?, ?, ?)
            """,
            (
                client_order_id,
                run_id,
                ticker,
                side,
                notional,
                decision,
                confidence,
                decision_as_of,
            ),
        )


def _update_order(
    client_order_id: str,
    *,
    status: str | None = None,
    alpaca_order_id: str | None = None,
    filled_qty: float | None = None,
    filled_avg_price: float | None = None,
    error: str | None = None,
) -> None:
    """Patch a row with whatever a response path learned. A passed None is
    'no news', not an erasure."""
    fields = ["updated_at = datetime('now')"]
    params: list = []
    for column, value in (
        ("status", status),
        ("alpaca_order_id", alpaca_order_id),
        ("filled_qty", filled_qty),
        ("filled_avg_price", filled_avg_price),
        ("error", error),
    ):
        if value is not None:
            fields.append(f"{column} = ?")
            params.append(value)
    params.append(client_order_id)
    with _connect() as connection:
        connection.execute(
            f"UPDATE broker_orders SET {', '.join(fields)} WHERE client_order_id = ?",
            params,
        )


def _age_hours(as_of: str) -> float | None:
    if not as_of:
        return None
    try:
        stamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0)


# ---- submission (the safety core) -----------------------------------------


async def submit_order(run_id: str, ticker: str, side: str, notional: float) -> BrokerOrder:
    """Place one paper market/day notional order for a run's decision.

    Every guard runs before any HTTP call. The row is inserted with status
    pending_submit before the POST, so a timeout can only leave state
    'unknown', which refresh_order resolves by client_order_id - never a
    blind second submission.
    """
    from app import run_history

    ticker = ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        raise BrokerRuleError(f"invalid ticker symbol: '{ticker}'")
    if not settings.alpaca_configured:
        raise BrokerNotConfigured(
            "Alpaca paper trading is not configured; add the API key ID and "
            "secret key on the Settings page"
        )
    if not settings.alpaca_trading_enabled:
        raise BrokerRuleError(
            "paper order submissions are disabled (ALPACA_TRADING_ENABLED)",
            status_code=403,
        )
    if notional <= 0:
        raise BrokerRuleError("notional must be positive")
    if notional > settings.alpaca_max_order_usd:
        raise BrokerRuleError(
            f"notional must not exceed {settings.alpaca_max_order_usd:,.0f} USD"
        )
    # Alpaca accepts at most 2 decimal places on notional (42210000).
    notional = round(notional, 2)

    run = await run_history.get(run_id)
    if run is None:
        raise BrokerRuleError(f"run {run_id} not found", status_code=404)
    analysis = run.results.get(ticker)
    if analysis is None:
        raise BrokerRuleError(f"ticker {ticker} is not in run {run_id}", status_code=404)
    if analysis.error:
        raise BrokerRuleError(
            f"the analysis for {ticker} failed; there is no decision to trade",
            status_code=409,
        )
    side_for = {"BUY": "buy", "SELL": "sell"}.get(analysis.decision)
    if analysis.decision == "HOLD":
        raise BrokerRuleError("HOLD is never traded; there is nothing to submit")
    if side_for != side:
        raise BrokerRuleError(
            f"the decision for {ticker} is {analysis.decision}, not a {side}"
        )
    if side == "buy" and notional < 1:
        raise BrokerRuleError("buy notional must be at least 1 USD")
    age = _age_hours(analysis.as_of)
    if age is None or age > settings.alpaca_max_decision_age_hours:
        raise BrokerRuleError(
            "research too old to trade: reanalyze before ordering", status_code=409
        )

    snapshot = await account()
    if snapshot.status != "ACTIVE" or snapshot.trading_blocked:
        raise BrokerRuleError(
            "the paper account is not open for trading", status_code=403
        )

    if side == "sell":
        held = next(
            (p for p in await positions() if p.symbol == ticker), None
        )
        if held is None:
            raise BrokerRuleError(
                f"no paper position in {ticker} to sell; shorting is out of scope"
            )
        market_value = held.market_value or 0.0
        if market_value <= 0:
            raise BrokerRuleError(f"the {ticker} position has no value to sell")
        # A price tick between the position read and the POST can make an
        # exact-value sell overshoot the holding (Alpaca 40310000), so every
        # sell is haircut slightly: it can never short, but may leave dust.
        # ponytail: fixed 0.5% haircut; per-tick precision if dust ever matters.
        notional = round(min(notional, market_value * 0.995), 2)
        if notional < 0.01:
            raise BrokerRuleError(
                f"the {ticker} position is worth less than a cent at the live price"
            )

    if _has_active_order(run_id, ticker, side):
        raise BrokerRuleError(
            f"an order for {ticker} ({side}) in run {run_id} is already open",
            status_code=409,
        )
    if _non_rejected_orders_today() >= settings.alpaca_max_orders_per_day:
        raise BrokerRuleError(
            f"daily paper order cap reached ({settings.alpaca_max_orders_per_day})",
            status_code=429,
        )

    client_order_id = f"bta-{uuid.uuid4()}"
    await asyncio.to_thread(
        _insert_order,
        client_order_id,
        run_id,
        ticker,
        side,
        notional,
        analysis.decision,
        analysis.confidence,
        analysis.as_of,
    )
    return await _submit_market_order(client_order_id, ticker, side, notional)


async def _submit_market_order(
    client_order_id: str, ticker: str, side: str, notional: float
) -> BrokerOrder:
    """POST one market/day notional order and settle the local row from the
    response. The caller must have inserted the row before this runs."""

    def _submit():
        return _client().submit_order(
            MarketOrderRequest(
                symbol=ticker,
                notional=notional,
                side=OrderSide(side),
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                extended_hours=False,
                client_order_id=client_order_id,
            )
        )

    try:
        order = await asyncio.to_thread(_submit)
    except APIError as exc:
        message = _map_broker_error(exc)
        await asyncio.to_thread(
            _update_order, client_order_id, status="rejected", error=message
        )
        raise BrokerRuleError(message, status_code=502) from exc
    except requests.exceptions.RequestException as exc:
        # The POST may or may not have reached Alpaca: the only safe state is
        # 'unknown' and the only recovery is a lookup by client_order_id.
        logger.warning("[broker] submit %s lost connection: %s", client_order_id, exc)
        await asyncio.to_thread(
            _update_order, client_order_id, status="unknown", error="order state unknown, refreshing"
        )
        row = await asyncio.to_thread(_get_order_row, client_order_id)
        return _row_to_order(row)  # type: ignore[arg-type]

    await asyncio.to_thread(_update_from_remote_order, client_order_id, order)
    row = await asyncio.to_thread(_get_order_row, client_order_id)
    return _row_to_order(row)  # type: ignore[arg-type]


def _update_from_remote_order(client_order_id: str, order) -> None:
    """Copy the fields we track off an Alpaca order object into the row."""
    status = order.status
    status = str(status.value if isinstance(status, Enum) else status)
    filled_qty = order.filled_qty
    filled_price = order.filled_avg_price
    _update_order(
        client_order_id,
        status=status,
        alpaca_order_id=str(order.id),
        filled_qty=float(filled_qty) if filled_qty not in (None, "") else None,
        filled_avg_price=float(filled_price) if filled_price not in (None, "") else None,
    )


async def refresh_order(client_order_id: str) -> BrokerOrder:
    """Reconcile one local row against Alpaca by client_order_id.

    This is the single recovery path for pending_submit/unknown rows and the
    reconcile-on-read step for the orders list.
    """
    row = await asyncio.to_thread(_get_order_row, client_order_id)
    if row is None:
        raise BrokerRuleError(f"no order with client id {client_order_id}", status_code=404)

    def _fetch():
        return _client().get_order_by_client_id(client_order_id)

    try:
        order = await asyncio.to_thread(_fetch)
    except APIError as exc:
        message = _map_broker_error(exc)
        # 404 here means Alpaca never saw the submission: settle the row.
        if exc.status_code == 404:
            await asyncio.to_thread(
                _update_order, client_order_id, status="rejected", error=message
            )
            row = await asyncio.to_thread(_get_order_row, client_order_id)
            return _row_to_order(row)  # type: ignore[arg-type]
        raise BrokerRuleError(message, status_code=502) from exc

    await asyncio.to_thread(_update_from_remote_order, client_order_id, order)
    row = await asyncio.to_thread(_get_order_row, client_order_id)
    return _row_to_order(row)  # type: ignore[arg-type]


async def list_orders(limit: int = 50) -> list[BrokerOrder]:
    """Local rows newest first. Non-terminal rows are reconciled by
    client_order_id before they are returned; a page of 50 is well inside the
    200 calls/min limit, and an unreachable Alpaca degrades to the local row
    instead of blanking the history."""
    def _rows():
        with _connect() as connection:
            return connection.execute(
                "SELECT * FROM broker_orders ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    rows = await asyncio.to_thread(_rows)
    orders: list[BrokerOrder] = []
    for row in rows:
        if row["status"] not in TERMINAL_ORDER_STATUSES:
            try:
                orders.append(await refresh_order(row["client_order_id"]))
                continue
            except (BrokerRuleError, BrokerNotConfigured):
                pass  # serve the local row; the next read reconciles again
        orders.append(_row_to_order(row))
    return orders


async def equity(period: str = "1M") -> dict:
    """Paper-account equity timeseries for the performance card."""

    def _fetch():
        return _client().get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe="1D")
        )

    try:
        history = await asyncio.to_thread(_fetch)
    except APIError as exc:
        raise BrokerRuleError(_map_broker_error(exc), status_code=502) from exc

    stamps = list(history.timestamp or [])
    values = list(history.equity or [])
    # New accounts get zero-padded history before creation: those marks are
    # noise (a $0 baseline makes the change "+Infinity%"), so they are dropped.
    points = [
        (datetime.fromtimestamp(int(stamp), tz=timezone.utc).date().isoformat(), value)
        for stamp, value in zip(stamps, values)
        if value is not None and value > 0
    ]
    return {
        "period": period,
        "dates": [point[0] for point in points],
        "equity": [point[1] for point in points],
    }


async def order_performance(limit: int = 50) -> list[dict]:
    """Return since fill and alpha vs SPY for each filled order.

    The window for the SPY comparison is the order's own placement date to
    today, fetched once via get_closes_between; unpriced tickers are skipped
    rather than shown as a fake zero.
    """
    from datetime import date, timedelta

    from app.tools.market_data import get_closes_between, get_current_price

    def _rows():
        with _connect() as connection:
            return connection.execute(
                "SELECT * FROM broker_orders "
                "WHERE status = 'filled' AND filled_avg_price IS NOT NULL "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    rows = await asyncio.to_thread(_rows)
    if not rows:
        return []

    end = (date.today() + timedelta(days=1)).isoformat()
    start = min(row["created_at"][:10] for row in rows)
    results = await asyncio.gather(
        get_closes_between("SPY", start, end),
        *(get_current_price(row["ticker"]) for row in rows),
    )
    spy, prices = results[0], results[1:]

    performance: list[dict] = []
    for row, price in zip(rows, prices):
        if price is None:
            continue
        entry = float(row["filled_avg_price"])
        return_pct = round((price / entry - 1) * 100, 2)
        window = [
            close for day, close in spy.items() if day >= row["created_at"][:10]
        ]
        spy_pct = (
            round((window[-1] / window[0] - 1) * 100, 2) if len(window) >= 2 else None
        )
        performance.append(
            {
                "client_order_id": row["client_order_id"],
                "run_id": row["run_id"],
                "ticker": row["ticker"],
                "side": row["side"],
                "notional": row["notional"],
                "filled_qty": row["filled_qty"],
                "filled_avg_price": entry,
                "placed_at": row["created_at"],
                "current_price": round(price, 2),
                "return_pct": return_pct,
                "spy_return_pct": spy_pct,
                "alpha_pct": round(return_pct - spy_pct, 2)
                if spy_pct is not None
                else None,
            }
        )
    return performance


async def cancel_order(client_order_id: str) -> BrokerOrder:
    """Cancel a non-terminal order; 422 passes through when Alpaca says it
    can no longer be canceled."""
    row = await asyncio.to_thread(_get_order_row, client_order_id)
    if row is None:
        raise BrokerRuleError(f"no order with client id {client_order_id}", status_code=404)
    if row["status"] in TERMINAL_ORDER_STATUSES:
        raise BrokerRuleError(
            f"order {client_order_id} is already {row['status']}", status_code=422
        )
    if not row["alpaca_order_id"]:
        # Never submitted (or unknown without an id): settle locally.
        await asyncio.to_thread(
            _update_order, client_order_id, status="canceled", error=None
        )
        row = await asyncio.to_thread(_get_order_row, client_order_id)
        return _row_to_order(row)  # type: ignore[arg-type]

    def _cancel():
        _client().cancel_order_by_id(row["alpaca_order_id"])

    try:
        await asyncio.to_thread(_cancel)
    except APIError as exc:
        raise BrokerRuleError(_map_broker_error(exc), status_code=502) from exc

    return await refresh_order(client_order_id)
