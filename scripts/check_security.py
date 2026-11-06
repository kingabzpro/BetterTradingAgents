"""Security hardening checks (P2.23, OWASP-informed).

Covers the four guarantees that must hold on every push:
  1. DNS-rebinding regression: attacker Host headers (foreign domains,
     numeric-IP variants, suffix and port tricks) get 403 on every route,
     and the Origin guard still rejects mismatched origins.
  2. Security headers (CSP, nosniff, frame-deny, referrer policy) land on
     pages, static assets, API JSON, guard 403s, and 429s.
  3. Light per-endpoint rate limits: buckets trip a 429 with Retry-After,
     expensive endpoints get their own tighter buckets, health is exempt.
  4. Secrets never reach logs, SSE events, run history, or HTTP error text,
     even when provider exceptions carry key-bearing URLs (LLM02).

Run: uv run test security  (included in `test offline` and CI)
"""

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "security_test.db")
# Fake but realistically shaped secrets: every sink below is asserted
# against these exact values. Blank everything else so the machine's own
# credentials cannot leak into the run.
LLM_KEY = "sk-fake-llm-secret-0123456789abcdef"
FINNHUB_KEY = "fh-fake-token-1234567890abcdef"
WEBHOOK = "https://hooks.example.com/private/abcd1234efgh5678"
os.environ["LLM_API_KEY"] = LLM_KEY
os.environ["FINNHUB_API_KEY"] = FINNHUB_KEY
os.environ["WEBHOOK_URL"] = WEBHOOK
for name in (
    "OLOSTEP_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY", "BAKO_API_KEY",
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY",
    "BTA_KEYRING_SERVICE",
):
    os.environ[name] = ""

import httpx  # noqa: E402

from app import automation, chat, redact, run_history, runs, workflow  # noqa: E402
from app.main import _RATE_LIMITS, _rate_buckets, _rate_limit_for, app  # noqa: E402
from app.models import RunStatus, StockAnalysis  # noqa: E402

ATTACKER_HOSTS = [
    "evil.example.com",
    "attacker.io:8080",
    "2130706433",  # decimal encoding of 127.0.0.1
    "0x7f000001",  # hex encoding of 127.0.0.1
    "127.1",  # short-form loopback
    "127.0.0.1.evil.com",  # suffix trick
    "127.0.0.1.nip.io",  # public DNS resolving to loopback
    "localhost.evil.com",
]
LEGIT_HOSTS = ["127.0.0.1", "127.0.0.1:8000", "localhost", "LOCALHOST:8000", "[::1]"]
SAFE_GET_PATHS = ["/", "/settings", "/portfolio", "/history", "/api/health", "/api/features", "/api/runs"]


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def _assert_clean(blob: str, label: str) -> None:
    for secret in (LLM_KEY, FINNHUB_KEY, WEBHOOK):
        assert secret not in blob, f"secret leaked into {label}"


async def _raw_asgi_status(method: str, path: str, headers: list[tuple[bytes, bytes]]) -> int | None:
    """Fire one handcrafted ASGI scope at the app (httpx always injects a
    Host, so the missing-Host branch needs a raw scope to be reachable)."""
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("127.0.0.1", 80),
        "client": None,
        "headers": headers,
    }
    await app(scope, receive, send)
    for message in messages:
        if message["type"] == "http.response.start":
            return message["status"]
    return None


async def check_dns_rebinding(client: httpx.AsyncClient) -> None:
    """Every route must 403 every attacker Host header; the classic
    rebinding vector behind the 2025 MCP SDK CVE stays blocked (P2.11)."""
    requests: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or {"GET"}
        if path == "/static" or path.startswith("/static/"):
            requests.append(("GET", "/static/js/util.js"))
            continue
        for method in sorted(methods):
            if method == "HEAD":
                continue
            requests.append((method, path))
    checked = 0
    for host in ATTACKER_HOSTS:
        headers = {"Host": host} if host else {}
        for method, path in requests:
            response = await client.request(
                method, path, headers=headers, json={} if method != "GET" else None
            )
            assert response.status_code == 403, (
                f"attacker Host {host!r} on {method} {path} -> {response.status_code}, not 403"
            )
            checked += 1
    # Origin guard: a valid loopback Host but a foreign Origin is still an
    # attacker page (any browser POST carries Origin).
    response = await client.get(
        "/api/health", headers={"Origin": "https://evil.example.com"}
    )
    assert response.status_code == 403, f"foreign Origin -> {response.status_code}"
    # Missing Host (HTTP/1.0-style or crafted client): refused outright.
    status = await _raw_asgi_status("GET", "/api/health", [])
    assert status == 403, f"missing Host -> {status}, not 403"
    status = await _raw_asgi_status(
        "GET", "/api/health", [(b"host", b"127.0.0.1:8000")]
    )
    assert status == 200, f"loopback raw scope -> {status}"
    # Controls: loopback Host forms stay allowed, and a matching Origin too.
    for host in LEGIT_HOSTS:
        response = await client.get("/api/health", headers={"Host": host})
        assert response.status_code == 200, f"legit Host {host!r} -> {response.status_code}"
    response = await client.get(
        "/api/health", headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"}
    )
    assert response.status_code == 200, f"matching Origin -> {response.status_code}"
    for path in SAFE_GET_PATHS:
        response = await client.get(path)
        assert response.status_code != 403, f"legit request to {path} was blocked"
    print(f"dns rebinding OK: {checked} attacker replays blocked, controls pass")


async def check_security_headers(client: httpx.AsyncClient) -> None:
    for path in ("/", "/static/js/util.js", "/api/health"):
        response = await client.get(path)
        for name, expected in (
            ("Content-Security-Policy", None),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ):
            value = response.headers.get(name)
            assert value is not None, f"{name} missing on {path}"
            if expected:
                assert value == expected, f"{name} on {path} is {value!r}, want {expected!r}"
        csp = response.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp, f"CSP too loose on {path}: {csp}"
        assert "frame-ancestors 'none'" in csp, f"frame-ancestors missing on {path}"
    # The guard's own 403 must carry the headers too (attacker page learns nothing).
    response = await client.get("/", headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in response.headers
    print("security headers OK: pages, assets, JSON, and guard 403s all stamped")


async def check_rate_limits(client: httpx.AsyncClient) -> None:
    assert _rate_limit_for("POST", "/api/analyze") == ("POST /api/analyze", 20)
    assert _rate_limit_for("POST", "/api/runs/abc123/chat") == ("POST /api/runs/", 30)
    assert _rate_limit_for("GET", "/api/runs") == ("GET *", 600)
    assert _rate_limit_for("GET", "/api/health") == ("GET *", 600)

    _rate_buckets.clear()
    original = _RATE_LIMITS["GET"]
    _RATE_LIMITS["GET"] = 5
    try:
        codes = [
            (await client.get("/api/features")).status_code for _ in range(6)
        ]
        assert codes[:5] == [200] * 5, f"first burst rejected unexpectedly: {codes}"
        assert codes[5] == 429, f"6th request should trip the bucket: {codes}"
        blocked = await client.get("/api/features")
        assert blocked.headers.get("Retry-After"), "429 must carry Retry-After"
        # Health stays exempt even under an exhausted bucket.
        assert (await client.get("/api/health")).status_code == 200
    finally:
        _RATE_LIMITS["GET"] = original
        _rate_buckets.clear()
    print("rate limits OK: buckets trip 429 with Retry-After, health exempt")


async def check_secrets_never_logged(client: httpx.AsyncClient) -> None:
    # Unit: the redaction set covers every fake secret, and redact() scrubs it.
    for secret in (LLM_KEY, FINNHUB_KEY, WEBHOOK):
        assert secret in redact._secret_values(), "configured secret missing from redaction set"
    assert redact.redact(f"url?token={FINNHUB_KEY}&x=1") == "url?token=[redacted]&x=1"
    assert redact.redact(f"auth failed for {LLM_KEY}") == "auth failed for [redacted]"
    assert redact.redact("nothing sensitive here") == "nothing sensitive here"

    capture = _Capture()
    root = logging.getLogger()
    root.addHandler(capture)

    original_fetch = workflow.get_stock_data
    original_answer = chat.answer_question
    try:
        # Provider exceptions embed key-bearing URLs (Finnhub auth is a query
        # param): the failure must surface redacted everywhere it lands.
        async def leaky_fetch(ticker, forecast=None):
            raise RuntimeError(
                f"GET https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY} failed"
            )

        workflow.get_stock_data = leaky_fetch
        store = runs.RunStore()
        await run_history.init()
        run = await store.create(["NVDA"], client_id="securitycheck01", depth="fast")
        await run.execution_task
        assert run.status == "failed" and run.results["NVDA"].error

        _assert_clean(json.dumps(run.events, default=str), "SSE event replay")
        assert FINNHUB_KEY not in (run.error or ""), "secret leaked into run error"
        row = sqlite3.connect(os.environ["DB_PATH"]).execute(
            "SELECT error, results_json FROM analysis_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
        assert row is not None
        _assert_clean(" ".join(str(value) for value in row), "persisted run history")
        items = await store.list_history("securitycheck01", 10)
        _assert_clean(
            json.dumps([item.model_dump(mode="json") for item in items], default=str),
            "history listing",
        )

        # The chat 503 detail is the other browser-visible sink.
        analysis = StockAnalysis(ticker="NVDA", decision="HOLD", confidence=0.5)
        await run_history.save(
            RunStatus(
                run_id="chatsec001",
                tickers=["NVDA"],
                status="completed",
                started_at=1_780_000_000.0,
                results={"NVDA": analysis},
            ),
            completed_at=1_780_000_060.0,
            owner_id="securitycheck01",
        )

        async def leaky_answer(*args, **kwargs):
            raise RuntimeError(f"request to https://api.deepseek.com failed: key {LLM_KEY}")

        chat.answer_question = leaky_answer
        response = await client.post(
            "/api/runs/chatsec001/chat",
            json={"ticker": "NVDA", "messages": [{"role": "user", "content": "why hold?"}]},
            headers={"X-Client-ID": "securitycheck01"},
        )
        assert response.status_code == 503, f"chat failure should be a 503: {response.status_code}"
        _assert_clean(response.text, "chat error detail")

        # Webhook failures log the URL-bearing exception: redacted too.
        class LeakyClient:
            async def post(self, url, **kwargs):
                raise RuntimeError(f"connect failed to {url}")

        original_client = automation.get_client
        automation.get_client = lambda: LeakyClient()
        try:
            await automation._post_webhook(
                SimpleNamespace(
                    id=1, status="completed", trigger="check", tickers=[],
                    decisions={}, orders=[], error=None,
                )
            )
        finally:
            automation.get_client = original_client
    finally:
        workflow.get_stock_data = original_fetch
        chat.answer_question = original_answer
        root.removeHandler(capture)

    _assert_clean("\n".join(capture.records), "captured logs")
    print("secrets guard OK: keys absent from events, history, errors, and logs")


async def checks() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        _rate_buckets.clear()
        await check_dns_rebinding(client)
        _rate_buckets.clear()
        await check_security_headers(client)
        _rate_buckets.clear()
        await check_rate_limits(client)
        _rate_buckets.clear()
        await check_secrets_never_logged(client)
    print("ALL SECURITY CHECKS PASSED")


asyncio.run(checks())
