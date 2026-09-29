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
from enum import Enum

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient

from app.config import settings
from app.models import BrokerAccount, BrokerPosition, BrokerStatus

logger = logging.getLogger("broker")

PAPER_DOCS_URL = "https://docs.alpaca.markets/us/docs/paper-trading"

# Terminal for our purposes; anything else may still move or be canceled.
TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected"}


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
            "Alpaca paper trading is not configured; set ALPACA_API_KEY_ID "
            "and ALPACA_API_SECRET_KEY in .env and restart"
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
    )


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
    """Startup hook. Reads stay available regardless of the kill switch."""
    if not settings.alpaca_configured:
        logger.info("[broker] paper trading not configured (dormant)")
    else:
        logger.info(
            "[broker] Alpaca paper connection configured | submissions=%s",
            "on" if settings.alpaca_trading_enabled else "off (kill switch)",
        )
