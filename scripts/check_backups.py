"""Backup checks: consistent snapshots, retention, API surface (P2.2).

Run: uv run test backups
"""

import os
import tempfile
from pathlib import Path

# Isolated DB and cleared env before app.config is imported. The backup
# directory gets its own scratch folder via the same env knob the app reads.
_TMP = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(_TMP / "backups_test.db")
os.environ["BACKUP_DIR"] = str(_TMP / "backups")
os.environ["LLM_API_KEY"] = ""
os.environ["ALPACA_API_KEY"] = ""
os.environ["ALPACA_SECRET_KEY"] = ""

import httpx  # noqa: E402

from app import backup, broker, run_history  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


def _files() -> list[str]:
    return sorted(p.name for p in backup.backup_dir().iterdir() if p.is_file())


async def main() -> None:
    await run_history.init()
    await broker.init()

    assert backup.backup_dir() == Path(os.environ["BACKUP_DIR"])
    assert backup.list_backups() == []

    first = backup.create_backup()
    assert first["size_bytes"] > 0
    assert first["name"].startswith("portfolio-") and first["name"].endswith(".db")
    assert [b["name"] for b in backup.list_backups()] == [first["name"]]
    # The snapshot is a real SQLite database that opens and holds the schema.
    import sqlite3

    copy = backup.backup_dir() / first["name"]
    connection = sqlite3.connect(copy)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "broker_orders" in tables
    print("create_backup: consistent readable snapshot OK")

    second = backup.create_backup()
    assert [b["name"] for b in backup.list_backups()] == [second["name"], first["name"]]
    print("list_backups newest-first OK")

    # Retention keeps only the newest BACKUP_KEEP portfolio-*.db files; files
    # that do not match the naming convention are never touched.
    stranger = backup.backup_dir() / "notes.txt"
    stranger.write_text("not a backup", encoding="utf-8")
    settings.backup_keep = 2
    backup.create_backup()
    kept = [n for n in _files() if n != "notes.txt"]
    assert len(kept) == 2, kept
    settings.backup_keep = 1
    backup.create_backup()
    assert [n for n in _files() if n != "notes.txt"] == [b["name"] for b in backup.list_backups()]
    assert stranger.exists()
    print("retention prunes only portfolio-*.db OK")

    # The API surface: list, and run one on demand.
    settings.owner_token = ""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        response = await c.get("/api/backups")
        assert response.status_code == 200
        payload = response.json()
        assert payload["dir"] == str(backup.backup_dir())
        assert payload["backups"] == backup.list_backups()
        response = await c.post("/api/backups")
        assert response.status_code == 200
        created = response.json()
        assert created["size_bytes"] > 0
        assert backup.list_backups()[0]["name"] == created["name"]
    print("backups API list + create OK")

    print("ALL BACKUP CHECKS PASSED")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
