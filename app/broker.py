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
import json
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from enum import Enum

import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    MarketOrderRequest,
)

from app.config import settings
from app.models import (
    BrokerAccount,
    BrokerOrder,
    BrokerPosition,
    BrokerStatus,
    EquityMark,
)
from app.db import connect as _connect

logger = logging.getLogger("broker")

PAPER_DOCS_URL = "https://docs.alpaca.markets/us/docs/paper-trading"

TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")

# Terminal for our purposes; anything else may still move or be canceled.
TERMINAL_ORDER_STATUSES = ("filled", "canceled", "expired", "rejected")

# Transient broker failures worth a retry on read paths (P2.3): rate limits
# and server errors. Auth/validation errors fail immediately.
TRANSIENT_STATUS_CODES = (429, 500, 502, 503, 504)
BROKER_READ_RETRIES = 2
BROKER_RETRY_BACKOFF_S = 0.5

# Local non-terminal rows older than this are no longer swept: a row that old
# would have settled through refresh_order's 404 path on an earlier sweep.
RECONCILE_WINDOW_DAYS = 7



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
    """Run one sync SDK call off the event loop, retrying transient failures.

    Rate limits, broker 5xx, and network drops are retried with a short
    backoff (P2.3); auth/validation errors map through immediately. Only
    reads go through here - the order POST path retries nothing blindly.
    """
    for attempt in range(BROKER_READ_RETRIES + 1):
        try:
            return await asyncio.to_thread(fn)
        except requests.exceptions.RequestException as exc:
            if attempt >= BROKER_READ_RETRIES:
                raise BrokerRuleError(
                    "Alpaca is unreachable right now; try again shortly",
                    status_code=503,
                ) from exc
            logger.warning(
                "[broker] read failed (%s); retry %d/%d",
                exc, attempt + 1, BROKER_READ_RETRIES,
            )
        except APIError as exc:
            if exc.status_code not in TRANSIENT_STATUS_CODES or attempt >= BROKER_READ_RETRIES:
                raise BrokerRuleError(_map_broker_error(exc), status_code=503) from exc
            logger.warning(
                "[broker] read hit HTTP %s; retry %d/%d",
                exc.status_code, attempt + 1, BROKER_READ_RETRIES,
            )
        await asyncio.sleep(BROKER_RETRY_BACKOFF_S * (attempt + 1))


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
    """Fresh paper-account snapshot (also the Test connection action).

    Every successful read ratchets the local equity high-water mark upward
    (P2.3 drawdown brake baseline), best-effort: a mark write must never
    fail the account read it rides on.
    """

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

    snapshot = await _call(_fetch)
    if snapshot.equity is not None:
        try:
            await asyncio.to_thread(_ratchet_high_water, snapshot.equity)
        except Exception as exc:  # noqa: BLE001 - best-effort by contract
            logger.warning("[broker] equity mark ratchet failed: %s", exc)
    return snapshot


def _ratchet_high_water(equity: float) -> None:
    """Raise the stored high-water mark to `equity`; never lower it.

    One statement so two concurrent account reads cannot interleave a
    read-modify-write: the WHERE guards the update on the row itself.
    """
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO equity_hwm (id, high_water, marked_at, note)
            VALUES (1, ?, datetime('now'), 'new equity high')
            ON CONFLICT(id) DO UPDATE SET
                high_water = excluded.high_water,
                marked_at = excluded.marked_at,
                note = excluded.note
            WHERE equity_hwm.high_water IS NULL
               OR excluded.high_water > equity_hwm.high_water
            """,
            (equity,),
        )


async def equity_mark() -> EquityMark:
    """The stored high-water mark, high_water=None before any account read."""

    def _read() -> EquityMark:
        with _connect() as connection:
            row = connection.execute(
                "SELECT high_water, marked_at, note FROM equity_hwm WHERE id = 1"
            ).fetchone()
        return EquityMark(
            high_water=row["high_water"] if row else None,
            marked_at=(row["marked_at"] if row else "") or "",
            note=(row["note"] if row else "") or "",
        )

    return await asyncio.to_thread(_read)


async def reset_equity_mark(note: str = "") -> EquityMark:
    """Re-baseline the mark to the account's current equity (P2.13).

    For a deposit or withdrawal: equity that moved for non-market reasons
    would otherwise read as drawdown (a withdrawal) or silently re-anchor
    high (a deposit, which the ratchet handles anyway). The note records why
    the baseline moved; the caller cannot set the mark to anything but the
    live equity, so this can never be used to disable the brake outright.
    """
    if not settings.alpaca_configured:
        raise BrokerNotConfigured("paper trading is not configured")
    snapshot = await account()
    if snapshot.equity is None:
        raise BrokerRuleError("paper account returned no equity to baseline against")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _write() -> EquityMark:
        with _connect() as connection:
            connection.execute(
                """
                UPDATE equity_hwm SET high_water = ?, marked_at = datetime('now'),
                                      note = ? WHERE id = 1
                """,
                (snapshot.equity, f"baseline reset to equity {stamp}: {note or 'deposit or withdrawal'}"),
            )
        return EquityMark(
            high_water=snapshot.equity, marked_at=stamp, note=note or "deposit or withdrawal"
        )

    return await asyncio.to_thread(_write)


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
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            try:
                connection.execute(
                    "ALTER TABLE broker_orders ADD COLUMN reason TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass  # column already exists on databases from before item 13
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_broker_orders_created "
                "ON broker_orders(created_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_sync (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    checked_at REAL,
                    in_sync INTEGER NOT NULL DEFAULT 1,
                    reconciled INTEGER NOT NULL DEFAULT 0,
                    drift_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO broker_sync (id, checked_at) VALUES (1, NULL)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS equity_hwm (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    high_water REAL,
                    marked_at TEXT,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute("INSERT OR IGNORE INTO equity_hwm (id) VALUES (1)")

    await asyncio.to_thread(_ddl)
    if not settings.alpaca_configured:
        logger.info("[broker] paper trading not configured (dormant)")
    else:
        logger.info(
            "[broker] Alpaca paper connection configured | submissions=%s",
            "on" if settings.alpaca_trading_enabled else "off (kill switch)",
        )




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
        reason=row["reason"] or "",
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
    decision: str | None,
    confidence: float | None,
    decision_as_of: str,
    reason: str = "",
) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO broker_orders (
                client_order_id, run_id, ticker, side, notional,
                status, decision, confidence, decision_as_of, reason
            ) VALUES (?, ?, ?, ?, ?, 'pending_submit', ?, ?, ?, ?)
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
                reason,
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

# Serializes the guard checks through the row insert (P2.8): the open-order
# dedupe and the daily cap are read-then-act, and the insert that makes the
# next caller see them happens in a thread, so concurrent submits could all
# pass the guards before any row landed. The POST stays outside the lock.
_submit_lock = asyncio.Lock()


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
        market_value = await _held_value_or_raise(ticker)
        # A price tick between the position read and the POST can make an
        # exact-value sell overshoot the holding (Alpaca 40310000), so every
        # sell is haircut slightly: it can never short, but may leave dust.
        # ponytail: fixed 0.5% haircut; per-tick precision if dust ever matters.
        notional = round(min(notional, market_value * 0.995), 2)
        if notional < 0.01:
            raise BrokerRuleError(
                f"the {ticker} position is worth less than a cent at the live price"
            )

    async with _submit_lock:
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


async def _held_value_or_raise(ticker: str) -> float:
    """The exit-able market value of a held position, or a rule error.

    Shared by the decision-sell path and the risk-overlay exit path so both
    read the broker the same way.
    """
    held = next((p for p in await positions() if p.symbol == ticker), None)
    if held is None:
        raise BrokerRuleError(
            f"no paper position in {ticker} to sell; shorting is out of scope"
        )
    value = held.market_value or 0.0
    if value <= 0:
        raise BrokerRuleError(f"the {ticker} position has no value to sell")
    return value


async def submit_exit_order(ticker: str, notional: float, reason: str) -> BrokerOrder:
    """Place one paper exit order for the risk overlay (release batch 3,
    item 13).

    Same guards as submit_order minus the run/analysis binding: the overlay
    exits a position the broker reports, so there is no decision row to
    validate. The row carries run_id='overlay' and the trigger in `reason`,
    and every account-level rule (kill switch, order caps, haircut, daily
    cap) still applies.
    """
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
    notional = round(notional, 2)

    snapshot = await account()
    if snapshot.status != "ACTIVE" or snapshot.trading_blocked:
        raise BrokerRuleError(
            "the paper account is not open for trading", status_code=403
        )
    market_value = await _held_value_or_raise(ticker)
    notional = round(min(notional, market_value * 0.995), 2)
    if notional < 0.01:
        raise BrokerRuleError(
            f"the {ticker} position is worth less than a cent at the live price"
        )

    async with _submit_lock:
        if _has_active_order("overlay", ticker, "sell"):
            raise BrokerRuleError(
                f"an overlay exit for {ticker} is already open", status_code=409
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
            "overlay",
            ticker,
            "sell",
            notional,
            "SELL",
            None,
            "",
            reason,
        )
    return await _submit_market_order(client_order_id, ticker, "sell", notional)


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

    history = await _call(_fetch)

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
                # P2.9: a SELL's good outcome is the ticker underperforming
                # SPY after the exit, so its alpha sign is a BUY's inverse.
                "alpha_pct": (
                    round((spy_pct - return_pct) if row["side"] == "sell" else (return_pct - spy_pct), 2)
                    if spy_pct is not None
                    else None
                ),
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


# ---- reconciliation (ROADMAP P2.3) -----------------------------------------

_sync_task: asyncio.Task | None = None


def _open_local_rows() -> list[sqlite3.Row]:
    with _connect() as connection:
        return connection.execute(
            "SELECT * FROM broker_orders "
            f"WHERE status NOT IN ({','.join('?' * len(TERMINAL_ORDER_STATUSES))}) "
            f"AND created_at >= datetime('now', '-{RECONCILE_WINDOW_DAYS} days')",
            TERMINAL_ORDER_STATUSES,
        ).fetchall()


def _has_order_history() -> bool:
    """True once this install has ever recorded an order. An install with an
    empty broker_orders table has never traded here, so an open order at the
    shared broker belongs to some other surface (another install, a manual
    trade), not to a crashed write of ours."""
    with _connect() as connection:
        return connection.execute("SELECT 1 FROM broker_orders LIMIT 1").fetchone() is not None


def _read_sync_state() -> dict:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM broker_sync WHERE id = 1").fetchone()
    if row is None:
        return {"checked_at": None, "in_sync": True, "reconciled": 0, "drift": [], "error": None}
    try:
        drift = json.loads(row["drift_json"] or "[]")
    except ValueError:
        drift = []
    return {
        "checked_at": row["checked_at"],
        "in_sync": bool(row["in_sync"]),
        "reconciled": int(row["reconciled"]),
        "drift": drift,
        "error": row["error"],
    }


def _write_sync_state(state: dict) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE broker_sync SET checked_at = ?, in_sync = ?, reconciled = ?, "
            "drift_json = ?, error = ? WHERE id = 1",
            (
                state["checked_at"],
                int(state["in_sync"]),
                state["reconciled"],
                json.dumps(state["drift"]),
                state["error"],
            ),
        )


async def sync_state(max_age_s: float = 0.0) -> dict:
    """The stored sweep result; with `max_age_s`, refresh once when staler,
    so a freshly opened page does not show a stale banner but polling tabs
    still hit the broker at most once a minute."""
    state = await asyncio.to_thread(_read_sync_state)
    if max_age_s and settings.alpaca_configured and (
        state["checked_at"] is None
        or time.time() - state["checked_at"] > max_age_s
    ):
        try:
            return await sync_once()
        except (BrokerRuleError, BrokerNotConfigured) as exc:
            return {**state, "error": str(exc)}
    return state


async def sync_once() -> dict:
    """One reconciliation sweep (P2.3).

    Every non-terminal local row is refreshed against Alpaca by client
    order id: partial fills land in the row, and rows a crash left
    pending_submit/unknown settle honestly (filled, or rejected when Alpaca
    never saw them). Then Alpaca's own open orders are diffed against the
    local set: anything open at the broker that this app has no row for is
    drift, reported through the out-of-sync banner. Stores and returns the
    state.
    """
    if not settings.alpaca_configured:
        return await sync_state()
    reconciled = 0
    rows = await asyncio.to_thread(_open_local_rows)
    for row in rows:
        try:
            await refresh_order(row["client_order_id"])
            reconciled += 1
        except (BrokerRuleError, BrokerNotConfigured):
            pass  # broker unreachable: the next sweep retries the row
        except Exception as exc:  # noqa: BLE001 - one bad row must not kill the sweep
            logger.warning(
                "[broker] could not reconcile %s: %s", row["client_order_id"], exc
            )
    try:
        remote = await _call(
            lambda: _client().get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
            )
        )
    except (BrokerRuleError, BrokerNotConfigured) as exc:
        state = {
            "checked_at": time.time(),
            "in_sync": False,
            "reconciled": reconciled,
            "drift": [],
            "error": str(exc),
        }
        await asyncio.to_thread(_write_sync_state, state)
        return state
    local_ids = {row["client_order_id"] for row in rows}
    drift = []
    for order in remote:
        client_order_id = str(getattr(order, "client_order_id", "") or "")
        if not client_order_id or client_order_id in local_ids:
            continue
        drift.append(
            {
                "client_order_id": client_order_id,
                "symbol": str(getattr(order, "symbol", "") or ""),
                "side": _text(getattr(order, "side", "")),
                "notional": _f(getattr(order, "notional", None)),
                "submitted_at": str(getattr(order, "submitted_at", "") or ""),
            }
        )
    if drift and not await asyncio.to_thread(_has_order_history):
        # This install has never recorded an order, so the open order(s) at
        # the broker were placed by another surface sharing the account (a
        # second install, a manual trade). Nothing of ours can be out of
        # sync: report quietly instead of raising the drift banner at users
        # who never opted into trading here.
        logger.info(
            "[broker] %d open order(s) at the broker were not placed by this "
            "install (no local order history); not flagged as drift",
            len(drift),
        )
        drift = []
    state = {
        "checked_at": time.time(),
        "in_sync": not drift,
        "reconciled": reconciled,
        "drift": drift,
        "error": None,
    }
    await asyncio.to_thread(_write_sync_state, state)
    if drift:
        logger.warning(
            "[broker] out of sync: %d open order(s) at the broker with no local row",
            len(drift),
        )
    return state


async def reconcile_startup() -> None:
    """Recovery sweep at startup, before any new submission can be accepted:
    rows a crashed process left pending_submit/unknown settle here."""
    if not settings.alpaca_configured:
        return
    try:
        await sync_once()
    except Exception as exc:  # noqa: BLE001 - startup must not die on the broker
        logger.warning("[broker] startup reconciliation failed: %s", exc)


async def _sync_loop() -> None:
    # Sleep-first: a sweep fires only after a full interval, so short-lived
    # processes (the offline checks) never reach the broker.
    logger.info("[broker] sync loop started (every %d min)", settings.broker_sync_interval_minutes)
    while True:
        await asyncio.sleep(settings.broker_sync_interval_minutes * 60)
        try:
            await sync_once()
        except Exception as exc:  # noqa: BLE001 - the loop outlives any bug
            logger.warning("[broker] sync sweep failed: %s", exc)


def start_sync_loop() -> None:
    """Spawn the interval reconciliation sweep (idempotent; app startup)."""
    global _sync_task
    if not settings.alpaca_configured:
        return
    if _sync_task is None or _sync_task.done():
        _sync_task = asyncio.create_task(_sync_loop(), name="broker-sync")
