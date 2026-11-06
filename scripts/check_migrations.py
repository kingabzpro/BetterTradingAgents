"""Migration runner checks: versioned upgrade and tested rollback (P2.2).

The release gate for P2.2 is explicit: migration rollback must be tested.
This check walks a scratch database through the whole cycle - upgrade to
head, write data through the new columns, roll back to zero (data intact,
new columns gone), upgrade again (old rows read fine, new writes work) -
so a broken `down` can never hide behind an untested code path.

Run: uv run test migrations
"""

import os
import tempfile
from pathlib import Path

# Isolated DB and cleared env before app.config is imported.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "migrations_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["ALPACA_API_KEY"] = ""
os.environ["ALPACA_SECRET_KEY"] = ""

from app import migrations, run_history  # noqa: E402
from app import broker  # noqa: E402
from app.db import connect  # noqa: E402


def _columns(table: str) -> set[str]:
    with connect() as connection:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


async def main() -> None:
    # The module inits create the tables migrations alter - the same order
    # startup uses.
    await run_history.init()
    await broker.init()

    with connect() as connection:
        assert migrations.current_version(connection) == 0
    version = migrations.apply_latest()
    assert version == migrations.LATEST, version
    assert "note" in _columns("broker_orders")
    # Idempotent: a second pass applies nothing and lands on the same version.
    assert migrations.apply_latest() == migrations.LATEST
    print(f"upgrade to head OK (v{version}, note column present, idempotent)")

    # Write through the new column the way the auto-exit path does.
    broker._insert_order(
        "bta-mig-1", "auto-exit", "NVDA", "sell", 5.0, 500.0,
        "SELL", None, None, note="auto-exit: stop-loss",
    )
    with connect() as connection:
        row = connection.execute(
            "SELECT note FROM broker_orders WHERE client_order_id = 'bta-mig-1'"
        ).fetchone()
        assert row["note"] == "auto-exit: stop-loss"
    print("write via note column OK")

    # Rollback to zero: the new column is gone, the pre-existing row and its
    # data are not. A down that dropped the table would fail right here.
    with connect() as connection:
        assert migrations.rollback(connection, 0) == 0
        assert "note" not in _columns("broker_orders")
        row = connection.execute(
            "SELECT run_id, ticker, side FROM broker_orders WHERE client_order_id = 'bta-mig-1'"
        ).fetchone()
        assert row is not None and row["ticker"] == "NVDA"
    print("rollback to v0 OK (column gone, data intact)")

    # Re-upgrade: the column is back, the old row reads as NULL, and new
    # writes work again. The order-row reader tolerates the missing column
    # either way, which the rollback window above relies on.
    migrations.apply_latest()
    assert "note" in _columns("broker_orders")
    broker._insert_order(
        "bta-mig-2", "auto-exit", "AMD", "sell", 2.0, 200.0,
        "SELL", None, None, note="auto-exit: take-profit",
    )
    orders = {o.client_order_id: o for o in await broker.list_orders()}
    assert orders["bta-mig-1"].note is None
    assert orders["bta-mig-2"].note == "auto-exit: take-profit"
    print("re-upgrade OK (old row NULL note, new writes and reads work)")

    # Targeted step: migrate to 0 then to 1 explicitly, matching what a
    # rollback-then-upgrade deployment would do.
    with connect() as connection:
        migrations.rollback(connection, 0)
        assert migrations.migrate(connection, 1) == 1
        assert migrations.current_version(connection) == 1
    print("targeted migrate(target=1) OK")

    print("ALL MIGRATIONS CHECKS PASSED")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
