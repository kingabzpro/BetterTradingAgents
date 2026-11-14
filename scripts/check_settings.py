"""Offline checks for the settings store, the settings API, and the
localhost drive-by guard.

Run: PYTHONPATH=. uv run python scripts/check_settings.py
No network and no real keychain: keyring is replaced with an in-memory fake
(and forced unavailable for the fallback scenario) and the DB is a temp
file, so every scenario is deterministic and key-less.
"""

import os
import sys
import tempfile
from pathlib import Path

# Isolated DB and cleared env before app.config is imported.
_TMP = Path(tempfile.mkdtemp()) / "settings_test.db"
os.environ["DB_PATH"] = str(_TMP)
for _var in (
    "LLM_API_KEY",
    "FINNHUB_API_KEY",
    "OLOSTEP_API_KEY",
    "TAVILY_API_KEY",
    "FIRECRAWL_API_KEY",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
):
    os.environ[_var] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app import settings_store  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

SECRET_VALUE = "sk-check-secret-never-leaks"


class _FakeKeyring:
    """In-memory stand-in for the OS credential vault."""

    def __init__(self):
        self.entries = {}

    def get_password(self, service, name):
        return self.entries.get(f"{service}/{name}")

    def set_password(self, service, name, value):
        self.entries[f"{service}/{name}"] = value

    def delete_password(self, service, name):
        self.entries.pop(f"{service}/{name}", None)


KEYRING = _FakeKeyring()
settings_store._keyring_mod = KEYRING  # skip backend discovery entirely

client = TestClient(app, base_url="http://127.0.0.1")

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"PASS  {label}")
    else:
        failed += 1
        print(f"FAIL  {label}{f': {detail}' if detail else ''}")


settings_store.apply_overrides()  # what startup does; also creates the table

# ---- snapshot: masking and source chips ------------------------------------

snapshot = client.get("/api/settings").json()
fields = {f["name"]: f for g in snapshot["groups"] for f in g["fields"]}
check("snapshot exposes groups", len(snapshot["groups"]) == 10)
check("experimental group in spec", any(g["key"] == "experimental" for g in snapshot["groups"]))
check("operations group in spec", any(g["key"] == "ops" for g in snapshot["groups"]))
check("snapshot masks unset secrets", fields["llm_api_key"]["source"] == "not set")
check("snapshot says keychain available", snapshot["keychain_available"] is True)

# ---- save: live apply, validation, keychain write ---------------------------

response = client.post(
    "/api/settings",
    json={"values": {"llm_api_key": SECRET_VALUE, "llm_model": "test-model-x"}},
)
body = response.json()
check("save returns 200", response.status_code == 200)
check("save never echoes the secret", SECRET_VALUE not in response.text)
check("secret applies live to settings", settings.llm_api_key == SECRET_VALUE)
check("non-secret applies live", settings.llm_model == "test-model-x")
check("secret lands in the keychain", KEYRING.get_password("BetterTradingAgents", "llm_api_key") == SECRET_VALUE)
check("secret stays out of the DB", "secret:llm_api_key" not in settings_store._db_secrets())
fields = {f["name"]: f for g in body["groups"] for f in g["fields"]}
check("secret now reports configured via keychain",
      fields["llm_api_key"]["configured"] is True and fields["llm_api_key"]["source"] == "keychain")

# Overrides survive a fresh apply_overrides (the restart path).
settings_store.apply_overrides()
check("overrides re-apply after restart", settings.llm_model == "test-model-x"
      and settings.llm_api_key == SECRET_VALUE)

# ---- validation: unknown, clamp, choices, bool ------------------------------

response = client.post("/api/settings", json={"values": {"nope": "x"}})
check("unknown setting rejected", response.status_code == 400)
response = client.post("/api/settings", json={"values": {"max_tickers": 999}})
check("out-of-range int rejected", response.status_code == 400)
response = client.post("/api/settings", json={"values": {"automation_depth": "bogus"}})
check("bad choice rejected", response.status_code == 400)
response = client.post("/api/settings", json={"values": {"debate_rounds": "2"}})
check("string ints coerce and clamp", response.status_code == 200 and settings.debate_rounds == 2)
response = client.post("/api/settings", json={"values": {"stream_reasoning": "true"}})
check("bool coerces", response.status_code == 200 and settings.stream_reasoning is True)

# ---- secrets: blank means unchanged, clear removes the entry ----------------

client.post("/api/settings", json={"values": {"finnhub_api_key": ""}})
check("blank secret leaves the keychain untouched",
      KEYRING.get_password("BetterTradingAgents", "llm_api_key") == SECRET_VALUE)

client.post("/api/settings", json={"clear_secrets": ["llm_api_key"]})
check("clear deletes the keychain entry", "BetterTradingAgents/llm_api_key" not in KEYRING.entries)
check("clear restores the .env default", settings.llm_api_key == settings_store.ENV_DEFAULTS["llm_api_key"])

# ---- fallback: no OS keychain -> unencrypted DB, flagged in the UI ----------

settings_store._keyring_mod = None
settings_store._keyring_error = "forced for check"
client.post("/api/settings", json={"values": {"llm_api_key": SECRET_VALUE}})
check("fallback writes the DB row", settings_store._db_secrets().get("llm_api_key") == SECRET_VALUE)
fallback_snapshot = client.get("/api/settings").json()
check("fallback flags itself", fallback_snapshot["keychain_available"] is False)
fields = {f["name"]: f for g in fallback_snapshot["groups"] for f in g["fields"]}
check("fallback source chip is db", fields["llm_api_key"]["source"] == "db")
check("fallback payload still masks", SECRET_VALUE not in fallback_snapshot and
      SECRET_VALUE not in client.get("/api/settings").text)
settings_store._keyring_mod = KEYRING
settings_store._keyring_error = ""

# ---- reset: everything back to .env -----------------------------------------

client.post("/api/settings", json={"values": {"llm_model": "another-model"}})
response = client.post("/api/settings/reset")
body = response.json()
check("reset restores .env values", settings.llm_model == settings_store.ENV_DEFAULTS["llm_model"])
check("reset clears the DB table", settings_store._db_overrides() == {})
check("reset wipes keychain entries", not any(k.endswith("/llm_api_key") for k in KEYRING.entries))
check("reset response masks secrets", SECRET_VALUE not in response.text)

# ---- full-spec round-trip: every setting saves exactly, nothing missing -----
# Owner ask 2026-10-23: walk EVERY field, not samples. Each non-secret gets a
# probe inside its own bounds; a value that does not apply exactly (wrong
# value or wrong type), or a spec field missing from the payload, fails.


def _probe(field: dict) -> object:
    default = settings_store.ENV_DEFAULTS[field["name"]]
    if field["type"] == "bool":
        return not bool(default)
    if field["type"] in ("int", "float"):
        for bound in (field["lo"], field["hi"]):
            if bound is not None and bound != default:
                return bound
        return default + 1
    if field["choices"]:
        return next(choice for choice in field["choices"] if choice != str(default))
    if field["name"].startswith("llm_base_url"):
        return "https://probe.example/v1"  # must clear the safe_base_url guard
    return f"probe-{field['name']}"


probes = {
    name: _probe(field)
    for name, field in settings_store.FIELDS.items()
    if not field["secret"]
}
response = client.post("/api/settings", json={"values": probes, "clear_secrets": []})
check("full-spec save accepted", response.status_code == 200, response.text[:200])
not_exact = [n for n, value in probes.items() if getattr(settings, n) != value]
check("every non-secret applies exactly (value and type)", not not_exact, str(not_exact))
snapshot = client.get("/api/settings").json()
read_back = {f["name"]: f.get("value") for g in snapshot["groups"] for f in g["fields"]}
missing = [n for n in probes if n not in read_back]
check("no spec field missing from the snapshot", not missing, str(missing))
drifted = [n for n, value in probes.items() if read_back[n] != value]
check("every saved value reads back exactly", not drifted, str(drifted))

secret_probes = {
    name: f"probe-{name}"
    for name, field in settings_store.FIELDS.items()
    if field["secret"]
}
client.post("/api/settings", json={"values": secret_probes, "clear_secrets": []})
snapshot = client.get("/api/settings").json()
secret_state = {f["name"]: f.get("configured") for g in snapshot["groups"] for f in g["fields"]}
unconfigured = [n for n in secret_probes if secret_state.get(n) is not True]
check("every secret saves and reports configured", not unconfigured, str(unconfigured))
client.post("/api/settings/reset")
not_restored = [n for n in probes if getattr(settings, n) != settings_store.ENV_DEFAULTS[n]]
check("reset restores every field to its .env default", not not_restored, str(not_restored))
print(f"full-spec round-trip OK ({len(probes)} non-secret + {len(secret_probes)} secret fields)")

# ---- drive-by guard ----------------------------------------------------------

check("loopback Host accepted", client.get("/api/health").status_code == 200)
check("non-loopback Host rejected",
      client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403)
check("cross-origin Origin rejected",
      client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 403)
check("same-origin Origin accepted",
      client.get("/api/health", headers={"Origin": "http://127.0.0.1"}).status_code == 200)
check("settings page served", client.get("/settings").status_code == 200)

# ---- P2.11: the socket peer is the unspoofable boundary ----------------------

import asyncio  # noqa: E402

from fastapi.responses import PlainTextResponse  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.config import safe_base_url  # noqa: E402
from app.main import local_only_guard  # noqa: E402


def _guard_response(peer):
    """Run the guard middleware on one fabricated request."""
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/api/health", "raw_path": b"/api/health", "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8000")],
        "client": (peer, 51000) if peer else None, "server": ("127.0.0.1", 8000),
    }

    async def call_next(request):
        return PlainTextResponse("ok")

    return asyncio.run(local_only_guard(Request(scope), call_next))


check("remote socket peer rejected", _guard_response("192.168.1.5").status_code == 403)
check("loopback socket peer accepted", _guard_response("127.0.0.1").status_code == 200)
check("IPv6 loopback peer accepted", _guard_response("::1").status_code == 200)
check("mapped-v6 loopback peer accepted", _guard_response("::ffff:127.0.0.1").status_code == 200)
check("synthetic peer name falls through to header checks",
      _guard_response("testclient").status_code == 200)
check("missing peer falls through", _guard_response(None).status_code == 200)

# ---- P2.11: base URLs the API key may follow ---------------------------------

check("https base URL accepted", safe_base_url("https://api.openai.com/v1"))
check("loopback http base URL accepted", safe_base_url("http://127.0.0.1:11434/v1"))
check("loopback-hostname http accepted", safe_base_url("http://localhost:8000/v1"))
check("IPv6 loopback http accepted", safe_base_url("http://[::1]:8080/v1"))
check("remote http base URL rejected", not safe_base_url("http://evil.example/v1"))
check("scheme-less base URL rejected", not safe_base_url("api.openai.com/v1"))

rejected = client.post("/api/settings", json={"values": {"llm_base_url": "http://evil.example/v1"}})
check("settings rejects a leaking base URL", rejected.status_code == 400)
check("rejection explains the leak", "cleartext" in rejected.json().get("detail", ""))
accepted = client.post(
    "/api/settings", json={"values": {"llm_base_url_analysts": "http://127.0.0.1:11434/v1"}}
)
check("settings accepts a loopback role URL", accepted.status_code == 200)
check("rejected URL was not applied", settings.llm_base_url != "http://evil.example/v1")
client.post("/api/settings/reset")  # roll the accepted override back for the rest

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
