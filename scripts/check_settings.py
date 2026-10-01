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
    "NIXTLA_API_KEY",
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
check("snapshot exposes groups", len(snapshot["groups"]) == 8)
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

# ---- drive-by guard ----------------------------------------------------------

check("loopback Host accepted", client.get("/api/health").status_code == 200)
check("non-loopback Host rejected",
      client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403)
check("cross-origin Origin rejected",
      client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 403)
check("same-origin Origin accepted",
      client.get("/api/health", headers={"Origin": "http://127.0.0.1"}).status_code == 200)
check("settings page served", client.get("/settings").status_code == 200)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
