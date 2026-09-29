"""Offline checks for the Alpaca paper-trading connection (ROADMAP P2.1 M1).

Run: PYTHONPATH=. uv run python scripts/check_paper_trading.py
No network and no broker calls: the Alpaca client seam is replaced with a
fake, so every scenario is deterministic and key-less.
"""

import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

# Isolated DB and cleared Alpaca env before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "paper_trading_test.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""
os.environ["ALPACA_API_KEY_ID"] = ""
os.environ["ALPACA_API_SECRET_KEY"] = ""
os.environ["ALPACA_TRADING_ENABLED"] = ""

import httpx  # noqa: E402
from alpaca.common.exceptions import APIError  # noqa: E402
from alpaca.trading.enums import AccountStatus  # noqa: E402

from app import broker  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

FAKE_KEY = "PKCHECKKEY"
FAKE_SECRET = "secret-check-value-must-never-leak"

REAL_CLIENT = broker._client


def _use(fake):
    broker._client = lambda: fake


def _restore():
    broker._client = REAL_CLIENT


def _api_error(status: int, code: int, message: str) -> APIError:
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status))
    return APIError(json.dumps({"code": code, "message": message}), http_error)


class _FakeClient:
    """The smallest object that quacks like TradingClient for one scenario."""

    def __init__(self, account=None, positions=None, error=None):
        self._account = account
        self._positions = positions
        self._error = error

    def get_account(self):
        if self._error:
            raise self._error
        return self._account

    def get_all_positions(self):
        if self._error:
            raise self._error
        return self._positions


def _raw_account(**overrides):
    raw = SimpleNamespace(
        account_number="PA-TEST-123",
        status=AccountStatus.ACTIVE,
        equity="101234.56",
        cash="51234.56",
        buying_power="202469.12",
        last_equity="100000.00",
        trading_blocked=False,
    )
    for key, value in overrides.items():
        setattr(raw, key, value)
    return raw


async def checks() -> None:
    # ---- unconfigured: 503 with a setup hint, secrets absent -----------------
    assert not settings.alpaca_configured
    status = await broker.status()
    assert status.configured is False and status.enabled is False
    assert "alpaca.markets" in status.paper_url

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        read = await client.get("/api/broker/status")
        assert read.status_code == 200 and read.json()["configured"] is False

        missing = await client.get("/api/broker/account")
        assert missing.status_code == 503, missing.text
        assert "ALPACA_API_KEY_ID" in missing.json()["detail"]

        positions = await client.get("/api/broker/positions")
        assert positions.status_code == 503
    print("unconfigured 503 + hint OK")

    # ---- configured: the client seam is paper-only and carries the keys ------
    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET
    settings.alpaca_trading_enabled = False
    try:
        real = broker._client()
        assert type(real).__name__ == "TradingClient"
        assert real._api_key == FAKE_KEY and real._secret_key == FAKE_SECRET
        base = real._base_url
        base = base.value if hasattr(base, "value") else base
        assert "paper-api.alpaca.markets" in str(base)
    finally:
        settings.alpaca_api_key_id = ""
        settings.alpaca_api_secret_key = ""
    print("paper-only client construction OK")

    # ---- mapping through the seam: account -----------------------------------
    fake = _FakeClient(account=_raw_account())
    _use(fake)
    try:
        account = await broker.account()
        assert account.account_number == "PA-TEST-123"
        assert account.status == "ACTIVE"
        assert account.equity == 101234.56 and account.cash == 51234.56
        assert account.buying_power == 202469.12 and account.last_equity == 100000.0
        assert account.trading_blocked is False
        print("account mapping OK")

        # Empty money fields stay None, never 0.
        empty = await _account_from(_raw_account(equity="", cash=None, trading_blocked=True))
        assert empty.equity is None and empty.cash is None
        assert empty.trading_blocked is True
        print("blank money fields -> None OK")
    finally:
        _restore()

    # ---- mapping through the seam: positions ---------------------------------
    fake = _FakeClient(
        positions=[
            SimpleNamespace(
                symbol="AAPL",
                qty="12",
                avg_entry_price="190.25",
                current_price="195.50",
                market_value="2346.00",
                unrealized_pl="63.00",
                unrealized_plpc="0.0276",
            ),
            SimpleNamespace(
                symbol="SPY",
                qty="1",
                avg_entry_price="500",
                current_price="",
                market_value="",
                unrealized_pl="",
                unrealized_plpc="",
            ),
        ]
    )
    _use(fake)
    try:
        rows = await broker.positions()
        assert rows[0].symbol == "AAPL" and rows[0].quantity == 12
        assert rows[0].avg_entry_price == 190.25 and rows[0].current_price == 195.5
        assert rows[0].market_value == 2346.0 and rows[0].unrealized_plpc == 0.0276
        assert rows[1].current_price is None and rows[1].unrealized_pl is None
        print("positions mapping OK")
    finally:
        _restore()

    # ---- error mapping --------------------------------------------------------
    cases = [
        (_api_error(401, 40110000, "bad key"), "check the ALPACA API keys"),
        (_api_error(429, 42910000, "rate limit"), "rate limit"),
        (_api_error(422, 42210000, "market closed"), "market closed"),
    ]
    for exc, expected in cases:
        text = broker._map_broker_error(exc)
        assert expected in text, (expected, text)
        assert FAKE_SECRET not in text
    print("error mapping OK")

    # ---- credentials never reach any payload ---------------------------------
    settings.alpaca_api_key_id = FAKE_KEY
    settings.alpaca_api_secret_key = FAKE_SECRET
    fake = _FakeClient(account=_raw_account(), positions=[])
    _use(fake)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/api/broker/status", "/api/broker/account", "/api/broker/positions"):
                body = (await client.get(path)).text
                assert FAKE_SECRET not in body and FAKE_KEY not in body, path
        print("secrets absent from every payload OK")
    finally:
        _restore()
        settings.alpaca_api_key_id = ""
        settings.alpaca_api_secret_key = ""


async def _account_from(raw) -> broker.BrokerAccount:
    _use(_FakeClient(account=raw))
    try:
        return await broker.account()
    finally:
        _restore()


asyncio.run(checks())
print("PAPER TRADING CHECKS PASSED")
