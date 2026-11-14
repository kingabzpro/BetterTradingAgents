"""Owner-gate checks: the shared-deployment token boundary (P2.2).

Covers both postures: without OWNER_TOKEN nothing changes (loopback-only
trust, no redirects), and with one set, every page and API call needs it -
via the X-Owner-Token header or the cookie /login hands out - while the
readiness probe, the login page, and the static assets stay reachable.

Run: uv run test owner_gate
"""

import os
import tempfile
from pathlib import Path

# Isolated DB and cleared env before app.config is imported.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "owner_gate_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["ALPACA_API_KEY"] = ""
os.environ["ALPACA_SECRET_KEY"] = ""
os.environ["OWNER_TOKEN"] = ""

import httpx  # noqa: E402

from app import automation, broker, memory, run_history, settings_store  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

TOKEN = "owner-check-token-123"


def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1")


async def main() -> None:
    # Startup events do not run under ASGITransport; create the tables the
    # gated endpoints read, in the same order startup does.
    await memory.init()
    await run_history.init()
    await broker.init()
    await automation.init()
    settings_store.apply_overrides()
    settings.owner_token = ""

    # ---- token unset: today's posture, no redirects, no 401s ---------------
    async with client() as c:
        assert (await c.get("/")).status_code == 200
        assert (await c.get("/api/settings")).status_code == 200
        assert (await c.get("/api/health")).status_code == 200
    print("no token configured: everything open as before OK")

    # ---- token set: pages redirect, API 401s, exempt paths stay open -------
    settings.owner_token = TOKEN
    async with client() as c:
        response = await c.get("/", follow_redirects=False)
        assert response.status_code == 307, response.status_code
        assert response.headers["location"].startswith("/login?next=")
        assert (await c.get("/api/settings")).status_code == 401
        assert (await c.get("/api/automation")).status_code == 401
        assert (await c.get("/api/metrics")).status_code == 401
        assert (await c.get("/api/backups")).status_code == 401
        assert (await c.post("/api/broker/orders", json={})).status_code == 401
        # The exempt surface: health polling, the login page and endpoints,
        # and the static assets the login page loads.
        assert (await c.get("/api/health")).status_code == 200
        assert (await c.get("/login")).status_code == 200
        assert (await c.get("/static/js/login.js")).status_code == 200
    print("token set: pages redirect, API 401s, exempt paths open OK")

    # ---- wrong token is rejected and grants nothing ------------------------
    async with client() as c:
        assert (await c.post("/api/owner/login", json={"token": "nope"})).status_code == 401
        assert (await c.get("/", follow_redirects=False)).status_code == 307
    print("wrong token rejected OK")

    # ---- no token configured: login says so instead of matching empty ------
    settings.owner_token = ""
    async with client() as c:
        assert (await c.post("/api/owner/login", json={"token": ""})).status_code == 422
    settings.owner_token = TOKEN
    print("login without a configured token rejected OK")

    # ---- correct token: cookie session works across pages and API ----------
    async with client() as c:
        response = await c.post("/api/owner/login", json={"token": TOKEN})
        assert response.status_code == 200
        assert "bta_owner" in response.headers.get("set-cookie", "")
        assert (await c.get("/")).status_code == 200
        assert (await c.get("/api/settings")).status_code == 200
        assert (await c.get("/api/metrics")).status_code == 200
        assert (await c.post("/api/backups")).status_code == 200
        # Logout drops the cookie and the gate closes again.
        assert (await c.post("/api/owner/logout")).status_code == 200
        assert (await c.get("/", follow_redirects=False)).status_code == 307
    print("cookie session round-trip and logout OK")

    # ---- header path: scripts send X-Owner-Token, no cookie needed ---------
    async with client() as c:
        assert (await c.get("/api/automation")).status_code == 401
        response = await c.get("/api/automation", headers={"X-Owner-Token": TOKEN})
        assert response.status_code == 200, response.status_code
        assert response.json()["exit_enabled"] is False
    print("X-Owner-Token header path OK")

    settings.owner_token = ""
    print("ALL OWNER-GATE CHECKS PASSED")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
