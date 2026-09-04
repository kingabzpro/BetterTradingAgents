"""Demo portfolio: SQLite persistence, prices via yfinance, no order execution.

Two kinds of positions share one table:
- demo trades (external = 0): added from the analysis page, funded by simulated cash;
- tracked holdings (external = 1): manually entered or CSV-imported real positions.
  They are valued at live prices but never touch the simulated cash balance.
"""

import asyncio
import logging
import re
import sqlite3

from app.config import settings
from app.models import (
    PortfolioImportItem,
    PortfolioImportResponse,
    PortfolioPosition,
    PortfolioSummary,
)
from app.tools.market_data import get_current_price

logger = logging.getLogger("portfolio")

_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


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
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                exit_price REAL,
                closed_at TEXT
            )
            """
        )
        # Databases created before close/exit/external-tracking support lack columns.
        for column in (
            "exit_price REAL",
            "closed_at TEXT",
            "external INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # column already exists


async def init() -> None:
    await asyncio.to_thread(_init_db)


def _cash_available() -> float:
    """Starting cash minus all demo buy costs plus all demo sell proceeds.

    Tracked holdings (external = 1) are excluded: they describe what the user
    already owns outside the simulation, so they neither consume nor free cash.
    """
    with _connect() as conn:
        open_cost, closed_cost, closed_proceeds = conn.execute(
            "SELECT "
            "COALESCE(SUM(quantity * entry_price) FILTER (WHERE closed_at IS NULL), 0), "
            "COALESCE(SUM(quantity * entry_price) FILTER (WHERE closed_at IS NOT NULL), 0), "
            "COALESCE(SUM(quantity * exit_price) FILTER (WHERE closed_at IS NOT NULL), 0) "
            "FROM positions WHERE external = 0"
        ).fetchone()
    return settings.starting_cash - open_cost - closed_cost + closed_proceeds


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
    cost = quantity * entry_price
    cash = await asyncio.to_thread(_cash_available)
    if cost > cash + 1e-9:
        raise ValueError(
            f"insufficient cash: position costs {cost:,.2f} but only {cash:,.2f} available"
        )
    position_id = await asyncio.to_thread(_insert, ticker, quantity, entry_price)
    logger.info("[portfolio] added %s x%.4f @ %.2f", ticker, quantity, entry_price)
    return PortfolioPosition(
        id=position_id,
        ticker=ticker,
        quantity=quantity,
        entry_price=round(entry_price, 2),
        current_price=entry_price,
        cost=round(cost, 2),
        value=round(cost, 2),
        pnl=0.0,
        pnl_pct=0.0,
    )


def _insert_external(rows: list[tuple[str, float, float]]) -> None:
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO positions (ticker, quantity, entry_price, external) "
            "VALUES (?, ?, ?, 1)",
            rows,
        )


async def import_positions(items: list[PortfolioImportItem]) -> PortfolioImportResponse:
    """Record externally tracked holdings (manual entry or CSV import).

    Unlike demo trades there is no cash guard: these positions already exist in
    the user's real account, so there is nothing to fund. Invalid rows are
    skipped and reported; valid rows are inserted in one transaction.
    """
    cleaned: list[PortfolioImportItem] = []
    errors: list[str] = []
    for item in items:
        ticker = item.ticker.strip().upper()
        if not _TICKER_RE.match(ticker):
            errors.append(f"invalid ticker symbol: '{item.ticker.strip()}'")
        elif item.quantity <= 0:
            errors.append(f"'{ticker}': quantity must be positive")
        else:
            cleaned.append(
                PortfolioImportItem(
                    ticker=ticker, quantity=item.quantity, entry_price=item.entry_price
                )
            )

    needs_price = [item for item in cleaned if item.entry_price is None]
    prices = await asyncio.gather(
        *(get_current_price(item.ticker) for item in needs_price)
    )
    live = dict(zip((item.ticker for item in needs_price), prices))

    rows: list[tuple[str, float, float]] = []
    for item in cleaned:
        entry_price = item.entry_price
        if entry_price is None:
            entry_price = live.get(item.ticker)
        if entry_price is None or entry_price <= 0:
            errors.append(f"'{item.ticker}': no valid entry price available")
        else:
            rows.append((item.ticker, item.quantity, entry_price))

    if rows:
        await asyncio.to_thread(_insert_external, rows)
        logger.info("[portfolio] imported %d tracked holding(s)", len(rows))
    return PortfolioImportResponse(imported=len(rows), errors=errors)


def _close(position_id: int, exit_price: float) -> dict | None:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM positions WHERE id = ? AND closed_at IS NULL",
            (position_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE positions SET exit_price = ?, closed_at = datetime('now') "
            "WHERE id = ?",
            (exit_price, position_id),
        )
        updated = conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        return dict(updated)


async def close_position(
    position_id: int, exit_price: float | None = None
) -> PortfolioPosition:
    row = await asyncio.to_thread(_select_open_row, position_id)
    if row is None:
        raise LookupError(f"no open position with id {position_id}")
    if exit_price is None:
        exit_price = await get_current_price(row["ticker"])
    if exit_price is None or exit_price <= 0:
        raise ValueError(f"no valid exit price available for '{row['ticker']}'")
    row = await asyncio.to_thread(_close, position_id, exit_price)
    if row is None:  # closed concurrently in the meantime
        raise LookupError(f"no open position with id {position_id}")
    logger.info(
        "[portfolio] closed %s x%.4f @ %.2f", row["ticker"], row["quantity"], exit_price
    )
    return _to_closed_position(row, exit_price)


def _select_open_row(position_id: int) -> dict | None:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, ticker, quantity, entry_price, added_at FROM positions "
            "WHERE id = ? AND closed_at IS NULL",
            (position_id,),
        ).fetchone()
        return dict(row) if row else None


def _to_closed_position(row: dict, exit_price: float) -> PortfolioPosition:
    cost = row["quantity"] * row["entry_price"]
    proceeds = row["quantity"] * exit_price
    return PortfolioPosition(
        id=row["id"],
        ticker=row["ticker"],
        quantity=row["quantity"],
        entry_price=round(row["entry_price"], 2),
        current_price=round(exit_price, 2),
        cost=round(cost, 2),
        value=round(proceeds, 2),
        pnl=round(proceeds - cost, 2),
        pnl_pct=round((exit_price / row["entry_price"] - 1) * 100, 2)
        if row["entry_price"]
        else None,
        added_at=row["added_at"],
        exit_price=round(exit_price, 2),
        closed_at=row.get("closed_at") or "",
    )


def _select_rows(open_only: bool = True) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM positions"
        if open_only:
            query += " WHERE closed_at IS NULL"
        else:
            query += " WHERE closed_at IS NOT NULL ORDER BY closed_at DESC, id DESC"
        return [dict(row) for row in conn.execute(query).fetchall()]


async def get_portfolio() -> PortfolioSummary:
    open_rows, closed_rows = await asyncio.gather(
        asyncio.to_thread(_select_rows, True),
        asyncio.to_thread(_select_rows, False),
    )
    tickers = sorted({row["ticker"] for row in open_rows})
    prices = await asyncio.gather(*(get_current_price(t) for t in tickers))
    price_map = dict(zip(tickers, prices))

    positions = []
    for row in open_rows:
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
                external=bool(row["external"]),
            )
        )

    history = [_to_closed_position(row, row["exit_price"]) for row in closed_rows]
    realized = round(sum(h.pnl or 0.0 for h in history), 2)

    # Totals cover positions with a live price; unpriced ones are counted so the
    # UI can say the number is partial instead of blanking the whole summary.
    known_value = sum(p.value for p in positions if p.value is not None)
    unpriced = sum(1 for p in positions if p.value is None)
    cash = await asyncio.to_thread(_cash_available)
    unrealized = sum(p.pnl or 0.0 for p in positions)
    return PortfolioSummary(
        starting_cash=settings.starting_cash,
        cash=round(cash, 2),
        positions_value=round(known_value, 2),
        total_equity=round(cash + known_value, 2),
        # Per-position gains (tracked holdings included, demo cash excluded) rather
        # than equity - starting_cash, which only holds for pure demo portfolios.
        total_pnl=round(unrealized + realized, 2),
        realized_pnl=realized,
        unpriced_count=unpriced,
        positions=positions,
        history=history,
    )
