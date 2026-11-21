"""Check Webull snapshot capture in scratch storage with an offline HTTP fixture."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from app.backtest.options import webull, webull_credentials

APP_KEY = "offline-sandbox-key"
APP_SECRET = "offline-sandbox-secret"
ACCESS_TOKEN = "offline-sandbox-token"
PROVIDER_SECRETS = (APP_KEY, APP_SECRET, ACCESS_TOKEN)
MARKET_TIME = datetime(2026, 5, 11, 14, 40, tzinfo=timezone.utc)
AAPL_CALL = "AAPL260522C00300000"
NVDA_CALL = "NVDA260522C00125000"
SYMBOLS = [AAPL_CALL, NVDA_CALL]
CREDENTIALS = webull_credentials.WebullCredentials(APP_KEY, APP_SECRET, ACCESS_TOKEN)
SNAPSHOT_QUERY = {"category": "US_OPTION", "symbols": ",".join(SYMBOLS)}


class ScratchVault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, slot):
        return self.values.get((service, slot))

    def set_password(self, service, slot, value):
        self.values[service, slot] = value


def check_credentials():
    vault = ScratchVault()
    service = "bta-offline-webull-check"
    with patch.object(webull_credentials, "_vault", return_value=vault), patch.dict(
        "os.environ", {"BTA_KEYRING_SERVICE": service}
    ):
        assert webull_credentials.load_credentials() is None
        with (
            patch.object(webull_credentials, "getpass", side_effect=[APP_KEY, APP_SECRET]),
            patch.object(webull_credentials.sys.stdin, "isatty", return_value=True),
            patch.object(webull_credentials.sys.stderr, "isatty", return_value=True),
        ):
            webull_credentials.store_credentials()
            try:
                webull_credentials.store_credentials()
            except webull_credentials.CredentialError as error:
                assert error.reason == "credentials_already_stored"
            else:
                raise AssertionError("a stored sandbox pair must not be overwritten")
        assert webull_credentials.load_credentials() == webull_credentials.WebullCredentials(APP_KEY, APP_SECRET)
        vault.set_password(f"{service}.WebullSandbox.AccessToken", webull_credentials.ACCESS_TOKEN_SLOT, ACCESS_TOKEN)
        loaded = webull_credentials.load_credentials()
        assert loaded == CREDENTIALS
        assert all(value not in repr(loaded) for value in PROVIDER_SECRETS)


class Clock:
    def __init__(self, base=MARKET_TIME):
        self.base = base
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def now(self):
        return self.base + timedelta(seconds=self.elapsed)

    async def sleep(self, seconds):
        assert seconds >= 0
        self.elapsed += seconds


def assert_sanitized(text):
    assert all(value not in text for value in PROVIDER_SECRETS)


def check_signature():
    headers = webull.signed_headers(webull.SNAPSHOTS_PATH, SNAPSHOT_QUERY, CREDENTIALS, MARKET_TIME, "offline-nonce")
    assert headers["x-signature"] == "/VKgWJawUQr5sQ/FJYjRGLc8PuU="
    assert headers["x-signature-algorithm"] == "HMAC-SHA1" and headers["x-version"] == "v3"
    assert headers["x-access-token"] == ACCESS_TOKEN and APP_SECRET not in str(headers)


def mock_quote(symbol, stamped, **changes):
    return {"symbol": symbol, "bid": "1.05", "ask": "1.10", "bid_size": "2", "ask_size": "3",
            "quote_time": int(stamped.timestamp() * 1000), **changes}


def check_quote_quality():
    old = MARKET_TIME - timedelta(minutes=15)
    previous = {}
    rows = [mock_quote(AAPL_CALL, old)]
    first = webull.quote_observations(SYMBOLS, rows, MARKET_TIME, previous)
    assert len(first) == 2 and [row["symbol"] for row in first] == SYMBOLS
    assert first[0]["provider_quote_time_ms"] == rows[0]["quote_time"]
    assert first[0]["bid"] == "1.05" and first[0]["ask"] == "1.10"
    assert first[0]["bid_size"] == 2 and first[0]["ask_size"] == 3
    assert "quote_stale" in first[0]["quality_flags"] and "missing_snapshot" in first[1]["quality_flags"]

    refetched = webull.quote_observations(SYMBOLS, rows, MARKET_TIME + timedelta(seconds=5), previous)
    assert refetched[0]["provider_quote_time_ms"] == first[0]["provider_quote_time_ms"]
    assert {"quote_stale", "quote_timestamp_unchanged"} <= set(refetched[0]["quality_flags"])
    regressed = webull.quote_observations(SYMBOLS, [mock_quote(AAPL_CALL, old - timedelta(seconds=10))], MARKET_TIME, previous)
    assert "quote_timestamp_regressed" in regressed[0]["quality_flags"]
    assert previous[AAPL_CALL] == first[0]["provider_quote_time_ms"]

    crossed = mock_quote(AAPL_CALL, MARKET_TIME + timedelta(minutes=1), bid="1.30", ask_size="0", delay_minutes=15)
    bad = webull.quote_observations(SYMBOLS, [crossed, mock_quote("TSLA260522C00250000", MARKET_TIME)], MARKET_TIME, {})
    assert {"quote_from_future", "zero_displayed_size", "invalid_two_sided_market", "provider_reports_delay"} <= set(bad[0]["quality_flags"])
    assert bad[1]["symbol"] == NVDA_CALL and "missing_snapshot" in bad[1]["quality_flags"]
    invalid = mock_quote(AAPL_CALL, MARKET_TIME, bid="", bid_size="NaN", price="9.99")
    del invalid["ask"]
    malformed = webull.quote_observations([AAPL_CALL], [invalid], MARKET_TIME, {})[0]
    assert "missing_ask" in malformed["quality_flags"] and "invalid_bid_size" in malformed["quality_flags"]
    assert malformed["ask"] is None and malformed["bid_size"] is None
    duplicate = webull.quote_observations([AAPL_CALL], rows * 2, MARKET_TIME, {})[0]
    assert "duplicate_snapshot_rows" in duplicate["quality_flags"]

    secret_echo = webull.quote_observations([AAPL_CALL], [mock_quote(AAPL_CALL, MARKET_TIME, bid=APP_SECRET)], MARKET_TIME, {})
    assert_sanitized(json.dumps(secret_echo, default=str))


def assert_read_only(request):
    assert request.method == "GET" and request.url.scheme == "https"
    assert request.url.host == webull.HOST == "api.sandbox.webull.com"
    assert request.url.path in (webull.CONTRACTS_PATH, webull.SNAPSHOTS_PATH)
    assert request.headers["x-app-key"] == APP_KEY and request.headers["x-access-token"] == ACCESS_TOKEN
    if request.url.path == webull.SNAPSHOTS_PATH:
        assert request.url.params["category"] == "US_OPTION"
        assert 1 <= len(request.url.params["symbols"].split(",")) <= 20


async def check_network_boundary():
    clock = Clock()
    request_times, attempts, nonces = [], [], []

    def fixture(request):
        assert_read_only(request)
        request_times.append(clock.monotonic())
        nonces.append(request.headers["x-signature-nonce"])
        if len(request_times) == 1:
            return httpx.Response(503, json={"message": APP_SECRET})
        if len(request_times) == 2:
            return httpx.Response(200, json=[mock_quote(AAPL_CALL, clock.now())])
        return httpx.Response(307, headers={"location": "https://unexpected.invalid/trading/orders/place"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture), follow_redirects=True) as http:
        provider = webull.WebullQuotes(http, CREDENTIALS, monotonic=clock.monotonic, now=clock.now, sleep=clock.sleep)
        for path in ("/trading/orders/place", "https://unexpected.invalid" + webull.SNAPSHOTS_PATH):
            try:
                await provider.request(path, {}, 60, attempts)
            except webull.WebullError:
                pass
            else:
                raise AssertionError("non-market-data requests must be refused")
        too_many = ",".join(f"AAPL260522C{index:08d}" for index in range(1, 22))
        try:
            await provider.request(webull.SNAPSHOTS_PATH, {**SNAPSHOT_QUERY, "symbols": too_many}, 60, attempts)
        except webull.WebullError as error:
            assert error.code == "invalid_snapshot_batch"
        else:
            raise AssertionError("the documented snapshot contract maximum must be enforced")
        assert not request_times
        await provider.request(webull.SNAPSHOTS_PATH, SNAPSHOT_QUERY, 60, attempts)
        assert len(request_times) == 2 and request_times[1] - request_times[0] >= webull.REQUEST_INTERVAL
        assert len(set(nonces)) == 2
        try:
            await provider.request(webull.SNAPSHOTS_PATH, SNAPSHOT_QUERY, 60, attempts)
        except webull.WebullError as error:
            assert_sanitized(str(error))
        else:
            raise AssertionError("snapshot redirects must be refused")
    assert len(request_times) == 3
    assert_sanitized(json.dumps(attempts, default=str))


async def check_retry_deadline():
    clock = Clock()
    requests = []

    def fixture(request):
        assert_read_only(request)
        requests.append(clock.monotonic())
        return httpx.Response(429, headers={"retry-after": "9999"}, json={"message": ACCESS_TOKEN})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as http:
        provider = webull.WebullQuotes(http, CREDENTIALS, monotonic=clock.monotonic, now=clock.now, sleep=clock.sleep)
        try:
            await provider.request(webull.SNAPSHOTS_PATH, SNAPSHOT_QUERY, 2, [])
        except webull.WebullError as error:
            assert error.code == "rate_limited"
            assert_sanitized(str(error))
        else:
            raise AssertionError("a provider throttle response cannot count as a snapshot")
    assert len(requests) == 1 and clock.elapsed <= 2


def contract(symbol, ticker, strike, expiry="2026-05-22", right="CALL"):
    return {"instrument_id": str(int(symbol[-8:]) * 10 + (1 if right == "CALL" else 2)),
            "symbol": symbol, "underlying_symbol": ticker,
            "root_symbol": ticker, "status": "LISTING", "expiration_date": expiry,
            "option_type": right, "strike_price": strike, "multiplier": "100",
            "deliverables": [{"asset_type": "STOCK", "symbol": ticker, "amount": "100"}]}


def check_lock(root):
    with webull.capture_lock(root):
        try:
            with webull.capture_lock(root):
                raise AssertionError("an overlapping capture entered the lock")
        except webull.WebullError as error:
            assert error.code == "capture_already_running"
    with webull.capture_lock(root):
        pass


async def check_discovery_pagination(root):
    clock = Clock()
    queries = []
    cursors = [f"opaque-contract-page-{index}" for index in range(webull.CONTRACT_PAGE_LIMIT)]
    expected = {"underlying_symbols": "AAPL,NVDA", "category": "US_OPTION", "option_type": "CALL",
                "status": "LISTING", "show_deliverables": "true",
                "start_date": "2026-05-18", "end_date": "2026-05-25"}

    def fixture(request):
        assert_read_only(request)
        if request.url.path == webull.SNAPSHOTS_PATH:
            assert request.url.params["symbols"].split(",") == SYMBOLS
            return httpx.Response(200, json=[mock_quote(symbol, clock.now()) for symbol in SYMBOLS])
        index = len(queries)
        assert index < webull.CONTRACT_PAGE_LIMIT
        query = dict(request.url.params)
        assert query == (expected if index == 0 else {**expected, "pagination_key": cursors[index - 1]})
        queries.append(query)
        if index == 0:
            # A returned page still needs the local DTE and option-side filters.
            rows = [contract("AAPL260515C00300000", "AAPL", "300", "2026-05-15"),
                    contract("NVDA260529C00125000", "NVDA", "125", "2026-05-29"),
                    contract("AAPL260522P00300000", "AAPL", "300", right="PUT")]
        elif index == 1:
            rows = [contract(AAPL_CALL, "AAPL", "300"), contract(NVDA_CALL, "NVDA", "125")]
        else:
            rows = []
        return httpx.Response(200, json={"data": rows, "pagination_key": cursors[index]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as http:
        receipt = await webull.collect("check", ["AAPL", "NVDA"], root, http=http, credentials=CREDENTIALS,
                                       monotonic=clock.monotonic, now=clock.now, sleep=clock.sleep)
    assert len(queries) == webull.CONTRACT_PAGE_LIMIT
    assert receipt["status"] == "complete"
    assert [entry["symbol"] for entry in receipt["contracts"]] == SYMBOLS
    assert "contract_page_truncated" in receipt["flags"]
    attempts = receipt["discovery_attempts"]
    assert len(attempts) == len(queries)
    assert not attempts[0]["pagination_used"]
    assert all(item["pagination_used"] for item in attempts[1:])
    assert all(item["query"] == expected for item in attempts)
    receipt_json = (root / "receipt.json").read_text(encoding="utf8")
    assert all(cursor not in receipt_json for cursor in cursors)
    assert_sanitized(receipt_json)


async def check_collection(root):
    clock = Clock()
    check_times, capture_times = [], []
    catalog = [contract(AAPL_CALL, "AAPL", "300"), contract(NVDA_CALL, "NVDA", "125"),
               contract("AAPL260515C00300000", "AAPL", "300", "2026-05-15"),
               contract("AAPL260522P00300000", "AAPL", "300", right="PUT")]

    def transport(clock, received, omit_once=False):
        def fixture(request):
            assert_read_only(request)
            if request.url.path == webull.CONTRACTS_PATH:
                ticker = request.url.params.get("underlying_symbols", request.url.params.get("root_symbol", ""))
                return httpx.Response(200, json={"data": [row for row in catalog if row["underlying_symbol"] in ticker]})
            received.append(clock.monotonic())
            symbols = request.url.params["symbols"].split(",")
            assert 1 <= len(symbols) <= 2 and set(symbols) <= set(SYMBOLS)
            rows = [mock_quote(symbol, clock.now() - timedelta(milliseconds=100)) for symbol in symbols]
            if omit_once and len(received) == 2:
                rows = []
            if omit_once and len(received) == 3 and rows:
                rows[0]["instrument_id"] = "999999"
            return httpx.Response(200, json=rows)
        return httpx.MockTransport(fixture)

    def unexpected_request(_request):
        raise AssertionError("a blocked check or capture attempted an HTTP request")

    closed = Clock(MARKET_TIME - timedelta(days=2))
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as http:
        missing = await webull.collect("check", ["AAPL"], root / "missing", http=http, credentials=None,
                                       monotonic=closed.monotonic, now=closed.now, sleep=closed.sleep)
        assert missing["status"] == "blocked" and missing["error"] == "sandbox_credentials_missing"
        blocked = await webull.collect("capture", ["AAPL"], root / "closed", http=http, credentials=CREDENTIALS,
                                       monotonic=closed.monotonic, now=closed.now, sleep=closed.sleep)
        assert blocked["status"] == "blocked" and blocked["error"] == "five_regular_session_minutes_required"
        for directory in ("missing", "closed"):
            assert_sanitized((root / directory / "receipt.json").read_text(encoding="utf8"))

    async with httpx.AsyncClient(transport=transport(closed, check_times)) as http:
        receipt = await webull.collect("check", ["AAPL", "NVDA"], root / "check", http=http, credentials=CREDENTIALS,
                                       monotonic=closed.monotonic, now=closed.now, sleep=closed.sleep)
        assert receipt["connection_verified"] and receipt["required_quote_fields_complete"]
        assert receipt["freshness_status"] == "outside_regular_session"
    assert len(check_times) == 1

    async with httpx.AsyncClient(transport=transport(clock, capture_times, omit_once=True)) as http:
        receipt = await webull.collect("capture", ["AAPL", "NVDA"], root / "capture", http=http, credentials=CREDENTIALS,
                                       monotonic=clock.monotonic, now=clock.now, sleep=clock.sleep)
    assert receipt["connection_verified"] and receipt["status"] == "partial"
    assert 2 <= len(capture_times) <= 60
    assert all(b - a >= webull.REQUEST_INTERVAL for a, b in zip(capture_times, capture_times[1:]))
    # The first snapshot observes the discovery request's pacing delay.
    assert all(b - a >= webull.SNAPSHOT_INTERVAL for a, b in zip(capture_times[1:], capture_times[2:]))
    assert max(capture_times) < webull.CAPTURE_SECONDS and clock.elapsed <= webull.CAPTURE_SECONDS
    assert clock.elapsed >= webull.CAPTURE_SECONDS - webull.REQUEST_INTERVAL
    directory = root / "capture"
    snapshots = (directory / "snapshots.jsonl").read_text(encoding="utf8")
    receipt_json = (directory / "receipt.json").read_text(encoding="utf8")
    batches = [json.loads(line) for line in snapshots.splitlines()]
    assert len(batches) == len(capture_times)
    assert len({batch["batch_id"] for batch in batches}) == len(batches)
    assert all(batch["request_at"] <= batch["received_at"] for batch in batches)
    assert all("missing_snapshot" in row["quality_flags"] for row in batches[1]["observations"])
    assert receipt["quality_flag_counts"]["instrument_id_mismatch"] == 1
    assert_sanitized(snapshots + receipt_json)


async def checks():
    check_credentials()
    check_signature()
    check_quote_quality()
    await check_network_boundary()
    await check_retry_deadline()
    with TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        root = Path(scratch)
        check_lock(root)
        await check_discovery_pagination(root / "pagination")
        await check_collection(root)


if __name__ == "__main__":
    asyncio.run(checks())
    print("Webull sandbox quote capture checks passed (offline fixture)")
