"""Offline checks for tracked holdings: manual entry, CSV-style bulk import, accounting.

Run: PYTHONPATH=. uv run python scripts/check_portfolio_import.py
"""

import asyncio
import os
from pathlib import Path
import sqlite3
import tempfile

# Isolated DB before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "portfolio_import_test.db"
os.environ["DB_PATH"] = str(_TMP)

import httpx  # noqa: E402

from app import portfolio as pf  # noqa: E402
from app.main import app  # noqa: E402
from app.models import PortfolioImportItem, PortfolioImportRequest  # noqa: E402


async def fake_price(ticker):
    if ticker == "BAD":
        return None  # simulates a symbol yfinance cannot price
    return {"AAPL": 190.0, "AMD": 110.0, "VOO": 480.0}.get(ticker, 100.0)


pf.get_current_price = fake_price  # keep the checks offline


async def checks() -> None:
    await pf.init()

    # ---- bulk import: valid rows, bad ticker, later-unpriceable symbol ---------
    result = await pf.import_positions(
        [
            PortfolioImportItem(ticker=" aapl ", quantity=50, entry_price=180.25),
            PortfolioImportItem(ticker="VOO", quantity=25, entry_price=465.80),
            PortfolioImportItem(ticker="BAD", quantity=3, entry_price=10.0),
            PortfolioImportItem(ticker="NOT A TICKER!", quantity=5, entry_price=10.0),
        ]
    )
    assert result.imported == 3, result
    assert len(result.errors) == 1 and "invalid ticker" in result.errors[0], result
    print("bulk import OK:", result.imported, "imported /", len(result.errors), "error(s)")

    summary = await pf.get_portfolio()
    # BAD resolves no live price -> it is counted as unpriced, not silently valued
    assert summary.unpriced_count == 1, summary.unpriced_count
    tracked = {p.ticker: p for p in summary.positions}
    assert set(tracked) == {"AAPL", "VOO", "BAD"}, sorted(tracked)
    assert all(p.external for p in summary.positions)
    assert tracked["AAPL"].entry_price == 180.25 and tracked["AAPL"].quantity == 50
    # tracked holdings never touch demo cash
    assert summary.cash == pf.settings.starting_cash, summary.cash
    # totals value only the priced positions and exclude them from nothing else
    assert summary.positions_value == round(50 * 190.0 + 25 * 480.0, 2), summary.positions_value
    # total P&L = per-position unrealized + realized (no realized trades yet)
    aapl_pnl = 50 * 190.0 - 50 * 180.25
    voo_pnl = 25 * 480.0 - 25 * 465.80
    assert summary.total_pnl == round(aapl_pnl + voo_pnl, 2), summary.total_pnl
    print(
        "tracked accounting OK: cash=%s value=%s pnl=%s unpriced=%s"
        % (summary.cash, summary.positions_value, summary.total_pnl, summary.unpriced_count)
    )

    # ---- manual add without an entry price uses the live price -----------------
    manual = await pf.import_positions(
        [PortfolioImportItem(ticker="AMD", quantity=4, entry_price=None)]
    )
    assert manual.imported == 1 and not manual.errors, manual
    summary = await pf.get_portfolio()
    amd = next(p for p in summary.positions if p.ticker == "AMD")
    assert amd.entry_price == 110.0, amd.entry_price
    print("manual add at live price OK: AMD @", amd.entry_price)

    # ---- unpriceable row without an entry price is reported, not stored --------
    rejected = await pf.import_positions(
        [PortfolioImportItem(ticker="BAD", quantity=2, entry_price=None)]
    )
    assert rejected.imported == 0 and "no valid entry price" in rejected.errors[0], rejected
    print("unpriceable add rejected OK:", rejected.errors[0])

    # ---- demo trades still consume cash and mix with tracked holdings ----------
    demo = await pf.add_position("NVDA", 10, entry_price=100.0)  # fake live price 100
    summary = await pf.get_portfolio()
    assert summary.cash == pf.settings.starting_cash - 1000.0, summary.cash
    nvda = next(p for p in summary.positions if p.ticker == "NVDA")
    assert not nvda.external and nvda.value == 1000.0
    assert summary.total_equity == round(summary.cash + summary.positions_value, 2)
    print("demo + tracked mix OK: cash=%s equity=%s" % (summary.cash, summary.total_equity))

    # ---- closing a tracked holding realizes P&L without minting demo cash ------
    closed = await pf.close_position(amd.id, exit_price=120.0)
    assert closed.pnl == 40.0, closed.pnl
    summary = await pf.get_portfolio()
    assert summary.cash == pf.settings.starting_cash - 1000.0, summary.cash
    assert summary.realized_pnl == 40.0, summary.realized_pnl
    assert summary.total_pnl == round(aapl_pnl + voo_pnl + 40.0, 2), summary.total_pnl
    print("tracked close OK: realized=%s cash unchanged" % summary.realized_pnl)

    # ---- API surface ------------------------------------------------------------
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post(
            "/api/portfolio/import",
            json={
                "positions": [
                    {"ticker": "msft", "quantity": 2, "entry_price": 300.0},
                    {"ticker": "TOO LONG SYMBOL X", "quantity": 1, "entry_price": 5.0},
                ]
            },
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["imported"] == 1 and len(ok.json()["errors"]) == 1, ok.json()

        empty = await client.post("/api/portfolio/import", json={"positions": []})
        assert empty.status_code == 422, empty.status_code

        bad_qty = await client.post(
            "/api/portfolio/import",
            json={"positions": [{"ticker": "IBM", "quantity": -3, "entry_price": 5.0}]},
        )
        assert bad_qty.status_code == 422, bad_qty.status_code

        listing = await client.get("/api/portfolio")
        assert listing.status_code == 200
        payload = listing.json()
        tickers = {p["ticker"] for p in payload["positions"]}
        assert {"AAPL", "VOO", "NVDA", "MSFT"}.issubset(tickers), tickers
        assert payload["unpriced_count"] == 1
        assert any(p["external"] is True for p in payload["positions"])
        page = await client.get("/portfolio")
        assert page.status_code == 200 and "Import from CSV" in page.text
    print("API checks OK: import, validation, listing, page")

    with sqlite3.connect(_TMP) as connection:
        flags = [flag for _, flag in connection.execute(
            "SELECT ticker, external FROM positions ORDER BY id"
        ).fetchall()]
    # 6 rows total: AAPL, VOO, BAD, AMD, MSFT tracked; NVDA is the demo trade
    assert flags.count(0) == 1 and flags.count(1) == 5, flags
    print("storage OK: %d rows, %d tracked / 1 demo" % (len(flags), flags.count(1)))


asyncio.run(checks())
print("PORTFOLIO IMPORT CHECKS PASSED")
