"""Demo portfolio: SQLite persistence, prices via yfinance, no order execution."""

import asyncio
import logging
import sqlite3

from app.config import settings
from app.models import PortfolioPosition, PortfolioSummary
from app.tools.market_data import get_current_price

logger = logging.getLogger("portfolio")


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(settings.db_path)


def _init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


async def init() -> None:
    await asyncio.to_thread(_init_db)


def _insert(ticker: str, quantity: float, entry_price: float) -> int:
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO positions (ticker, quantity, entry_price) VALUES (?, ?, ?)",
            (ticker, quantity, entry_price),
        )
        return int(cursor.lastrowid)


async def add_position(
    ticker: str, quantity: float, entry_price: float | None = None
) -> PortfolioPosition:
    ticker = ticker.strip().upper()
    if entry_price is None:
        entry_price = await get_current_price(ticker)
    if entry_price is None or entry_price <= 0:
        raise ValueError(f"no valid price available for '{ticker}'")
    position_id = await asyncio.to_thread(_insert, ticker, quantity, entry_price)
    logger.info("[portfolio] added %s x%.4f @ %.2f", ticker, quantity, entry_price)
    return PortfolioPosition(
        id=position_id,
        ticker=ticker,
        quantity=quantity,
        entry_price=round(entry_price, 2),
        current_price=entry_price,
        cost=round(quantity * entry_price, 2),
        value=round(quantity * entry_price, 2),
        pnl=0.0,
        pnl_pct=0.0,
    )


def _select_rows() -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, ticker, quantity, entry_price, added_at FROM positions ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]


async def get_portfolio() -> PortfolioSummary:
    rows = await asyncio.to_thread(_select_rows)
    tickers = sorted({row["ticker"] for row in rows})
    prices = await asyncio.gather(*(get_current_price(t) for t in tickers))
    price_map = dict(zip(tickers, prices))

    positions = []
    for row in rows:
        cost = row["quantity"] * row["entry_price"]
        price = price_map.get(row["ticker"])
        value = row["quantity"] * price if price is not None else None
        positions.append(
            PortfolioPosition(
                id=row["id"],
                ticker=row["ticker"],
                quantity=row["quantity"],
                entry_price=round(row["entry_price"], 2),
                current_price=round(price, 2) if price is not None else None,
                cost=round(cost, 2),
                value=round(value, 2) if value is not None else None,
                pnl=round(value - cost, 2) if value is not None else None,
                pnl_pct=round((value / cost - 1) * 100, 2)
                if value is not None and cost
                else None,
                added_at=row["added_at"],
            )
        )

    invested = sum(p.cost for p in positions)
    known_value = sum(p.value for p in positions if p.value is not None)
    has_unknown = any(p.value is None for p in positions)
    return PortfolioSummary(
        starting_cash=settings.starting_cash,
        cash=round(settings.starting_cash - invested, 2),
        positions_value=None if has_unknown else round(known_value, 2),
        total_equity=None
        if has_unknown
        else round(settings.starting_cash - invested + known_value, 2),
        total_pnl=None if has_unknown else round(known_value - invested, 2),
        positions=positions,
    )
