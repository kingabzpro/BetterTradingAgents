"""User-editable settings: non-secrets in SQLite, secrets in the OS keychain.

FIELD_GROUPS is the single spec behind the settings API and the settings
page: names, labels, types, clamps, choices, and the secret flag. Values
apply live - every module reads settings.X at call time, and broker builds
a fresh Alpaca client per call, so no restart is needed after a save.

Secrets (API keys) never touch the database: they go to the OS credential
vault via keyring (Windows Credential Manager, macOS Keychain, Linux
Secret Service) under the service name below. If no usable keychain
exists, they fall back to the local DB unencrypted and the UI says so.
Precedence per field: keychain/DB override > .env default. Reset drops
every override and restores the .env values captured at import.
"""

import logging
import sqlite3
import time
from typing import Any

from app.config import settings

logger = logging.getLogger("settings")

KEYRING_SERVICE = "BetterTradingAgents"
STR_LIMIT = 2048


def _field(
    name: str,
    env: str,
    label: str,
    type_: str = "str",
    secret: bool = False,
    choices: tuple[str, ...] | None = None,
    lo: float | None = None,
    hi: float | None = None,
    hint: str = "",
    section: str = "",
    example: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "env": env,
        "label": label,
        "type": type_,
        "secret": secret,
        "choices": choices,
        "lo": lo,
        "hi": hi,
        "hint": hint,
        "section": section,
        "example": example,
    }


def _role(prefix: str, label: str, blurb: str, model_example: str) -> list[dict[str, Any]]:
    return [
        _field(f"llm_model_{prefix}", f"LLM_MODEL_{prefix.upper()}", f"{label} model", hint=blurb, example=model_example),
        _field(f"llm_base_url_{prefix}", f"LLM_BASE_URL_{prefix.upper()}", f"{label} base URL", hint="Empty = use the global base URL", example="https://api.openai.com/v1"),
        _field(f"llm_api_key_{prefix}", f"LLM_API_KEY_{prefix.upper()}", f"{label} API key", secret=True, hint="Empty = use the global key", example="sk-..."),
    ]


FIELD_GROUPS: list[dict[str, Any]] = [
    {
        "key": "llm",
        "tab": "LLM",
        "label": "LLM endpoint",
        "fields": [
            _field("llm_base_url", "LLM_BASE_URL", "Base URL", hint="Any OpenAI-compatible endpoint: OpenAI, Z.AI, DeepSeek, Qwen, OpenRouter, vLLM...", example="https://api.openai.com/v1"),
            _field("llm_api_key", "LLM_API_KEY", "API key", secret=True, hint="Enables the agents; without it the app runs in mock mode", example="sk-..."),
            _field("llm_model", "LLM_MODEL", "Model", hint="Used by every role without an override", example="zai-org/GLM-5.3-Flash"),
            _field("llm_temperature", "LLM_TEMPERATURE", "Temperature", "float", lo=0.0, hi=2.0, hint="0 = steady, 1 = creative", example="0.2"),
            _field("llm_timeout_seconds", "LLM_TIMEOUT_SECONDS", "Timeout (seconds)", "float", lo=5, hi=600, hint="Per-request cap", example="90"),
            _field("llm_reasoning_effort", "LLM_REASONING_EFFORT", "Reasoning effort", hint="Provider-specific: none, low, medium, high (GLM uses none/low)", example="none"),
            _field("llm_price_in", "LLM_PRICE_IN", "Price in ($/1M tokens)", "float", lo=0, hi=10000, hint="0 = use the built-in list-price table", example="1.5"),
            _field("llm_price_out", "LLM_PRICE_OUT", "Price out ($/1M tokens)", "float", lo=0, hi=10000, hint="0 = use the built-in list-price table", example="6"),
        ],
    },
    {
        "key": "llm_roles",
        "tab": "Role models",
        "label": "Per-role model overrides",
        "fields": _role("manager", "Manager", "The final BUY/HOLD/SELL call: worth a stronger model", "zai-org/GLM-5.3")
        + [_field("llm_reasoning_effort_manager", "LLM_REASONING_EFFORT_MANAGER", "Manager reasoning effort", hint="Empty = global effort; high requests deeper reasoning on supported models", example="high")]
        + _role("analysts", "Analysts", "The 5 researchers: a cheap fast model works", "zai-org/GLM-5.3-Flash")
        + _role("debate", "Debate", "Bull and bear argue the case", "zai-org/GLM-5.3-Flash"),
    },
    {
        "key": "providers",
        "tab": "Providers",
        "label": "Market-data providers",
        "fields": [
            _field("finnhub_api_key", "FINNHUB_API_KEY", "Finnhub API key", secret=True, hint="Profiles, fundamentals, company news; falls back to yfinance"),
            _field("olostep_api_key", "OLOSTEP_API_KEY", "Olostep API key", secret=True, hint="News search and Reddit/StockTwits sentiment"),
            _field("nixtla_api_key", "NIXTLA_API_KEY", "Nixtla TimeGPT API key", secret=True, hint="5-day forecast; falls back to the local trend model"),
        ],
    },
    {
        "key": "analysis",
        "tab": "Analysis",
        "label": "Analysis limits",
        "fields": [
            _field("max_tickers", "MAX_TICKERS", "Max tickers", "int", lo=1, hi=20, hint="Tickers accepted in one analysis", example="5"),
            _field("debate_rounds", "DEBATE_ROUNDS", "Debate rounds", "int", lo=1, hi=3, hint="2 or more adds one bull/bear rebuttal exchange", example="2"),
            _field("stream_reasoning", "STREAM_REASONING", "Stream agent tokens", "bool", hint="Live agent tokens in the run view"),
        ],
    },
    {
        "key": "risk",
        "tab": "Risk",
        "label": "Risk and sizing",
        "fields": [
            _field("default_position_size", "DEFAULT_POSITION_SIZE", "Default position size ($)", "float", lo=1, hi=100000000, hint="BUY size when the analysis suggests none", example="10000"),
            _field("max_position_pct", "MAX_POSITION_PCT", "Max position", "float", lo=0.01, hi=1, hint="Largest single position, share of equity", example="0.1 (= 10%)"),
            _field("max_invested_pct", "MAX_INVESTED_PCT", "Max invested", "float", lo=0.01, hi=1, hint="Largest invested total, share of equity", example="0.6 (= 60%)"),
            _field("min_cash_pct", "MIN_CASH_PCT", "Min cash", "float", lo=0, hi=1, hint="Cash buffer the risk gate keeps, share of equity", example="0.1 (= 10%)"),
            _field("max_drawdown_pct", "MAX_DRAWDOWN_PCT", "Drawdown brake", "float", lo=0.01, hi=1, hint="BUYs halt when open positions' unrealized loss exceeds this share", example="0.15 (= 15%)"),
            _field("max_positions", "MAX_POSITIONS", "Max open positions", "int", lo=1, hi=50, hint="A BUY of a new ticker halts at this many open positions", example="10"),
            _field("max_correlated_pct", "MAX_CORRELATED_PCT", "Correlated-group cap", "float", lo=0, hi=1, hint="Warning-only cap for correlated holdings", example="0.25 (= 25%)"),
        ],
    },
    {
        "key": "memory",
        "tab": "Memory",
        "label": "Decision memory",
        "fields": [
            _field("memory_horizon_days", "MEMORY_HORIZON_DAYS", "Outcome horizon (days)", "int", lo=1, hi=365, hint="Days before a decision's outcome grades", example="21"),
            _field("memory_reflect_with_llm", "MEMORY_REFLECT_WITH_LLM", "LLM reflections", "bool", hint="Off writes deterministic lessons"),
            _field("calibration_min_observations", "CALIBRATION_MIN_OBSERVATIONS", "Min graded calls", "int", lo=1, hi=1000, hint="Track records need this many graded calls", example="30"),
        ],
    },
    {
        "key": "alpaca",
        "tab": "Alpaca",
        "label": "Alpaca paper trading",
        "fields": [
            _field("alpaca_api_key_id", "ALPACA_API_KEY_ID", "API key ID", secret=True, section="Account", hint="Paper key from the Alpaca dashboard", example="PK..."),
            _field("alpaca_api_secret_key", "ALPACA_API_SECRET_KEY", "Secret key", secret=True, section="Account", hint="Stored in your OS keychain, never shown again"),
            _field("alpaca_trading_enabled", "ALPACA_TRADING_ENABLED", "Allow order submissions", "bool", section="Account", hint="Kill switch: off = read-only"),
            _field("alpaca_max_order_usd", "ALPACA_MAX_ORDER_USD", "Max order ($)", "float", lo=1, hi=100000000, section="Guard rails", example="10000"),
            _field("alpaca_max_orders_per_day", "ALPACA_MAX_ORDERS_PER_DAY", "Max orders per day", "int", lo=1, hi=1000, section="Guard rails", hint="Non-rejected submissions per UTC day", example="20"),
            _field("alpaca_max_decision_age_hours", "ALPACA_MAX_DECISION_AGE_HOURS", "Max decision age (hours)", "float", lo=1, hi=720, section="Guard rails", hint="Older research cannot be traded", example="72"),
        ],
    },
    {
        "key": "autopilot",
        "tab": "Autopilot",
        "label": "Autopilot sessions",
        "fields": [
            _field("automation_interval_minutes", "AUTOMATION_INTERVAL_MINUTES", "Minutes between sessions", "int", lo=15, hi=10080, section="Schedule", hint="Minimum 15", example="240"),
            _field("automation_candidates", "AUTOMATION_CANDIDATES", "Candidates per session", "int", lo=1, hi=10, section="Schedule", hint="Shortlist size from the market scan", example="3"),
            _field("automation_session_timeout_minutes", "AUTOMATION_SESSION_TIMEOUT_MINUTES", "Session timeout (minutes)", "int", lo=5, hi=240, section="Schedule", hint="Longer sessions are cancelled and recorded failed", example="40"),
            _field("automation_min_confidence", "AUTOMATION_MIN_CONFIDENCE", "BUY confidence bar", "float", lo=0, hi=1, section="Trading rules", hint="BUYs below this are skipped; SELLs always act", example="0.65 (= 65%)"),
            _field("automation_outlook", "AUTOMATION_OUTLOOK", "Outlook", section="Trading rules", choices=("day_trade", "short_term", "long_term")),
            _field("automation_depth", "AUTOMATION_DEPTH", "Depth", section="Trading rules", choices=("fast", "pro", "max")),
            _field("automation_allow_sells", "AUTOMATION_ALLOW_SELLS", "Allow SELLs", "bool", section="Trading rules", hint="A SELL decision exits the held position"),
        ],
    },
]

FIELDS: dict[str, dict[str, Any]] = {
    field["name"]: {**field, "group": group["key"]}
    for group in FIELD_GROUPS
    for field in group["fields"]
}
SECRET_NAMES = [f["name"] for f in FIELDS.values() if f["secret"]]

# The .env baseline, captured once at import before any override is applied.
ENV_DEFAULTS: dict[str, Any] = {name: getattr(settings, name) for name in FIELDS}


# ---- keychain ---------------------------------------------------------------


_keyring_mod: Any = None
_keyring_error: str = ""


def _keyring() -> Any:
    """The keyring module, or None when no OS credential vault is usable."""
    global _keyring_mod, _keyring_error
    if _keyring_mod is None and not _keyring_error:
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring

            if isinstance(keyring.get_keyring(), FailKeyring):
                raise RuntimeError("no OS keychain backend")
            _keyring_mod = keyring
        except Exception as exc:  # noqa: BLE001 - any keyring failure means fallback
            _keyring_error = str(exc)
            logger.warning(
                "[settings] no OS keychain (%s); secrets are stored "
                "unencrypted in the local database",
                exc,
            )
    return _keyring_mod


def keychain_available() -> bool:
    return _keyring() is not None


def keychain_get(name: str) -> str | None:
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, name) or None
    except Exception as exc:  # noqa: BLE001 - a locked/failed vault reads as unset
        logger.warning("[settings] keychain read for %s failed: %s", name, exc)
        return None


def keychain_set(name: str, value: str) -> None:
    keyring = _keyring()
    if keyring is None:
        raise RuntimeError(_keyring_error or "no OS keychain")
    keyring.set_password(KEYRING_SERVICE, name, value)


def keychain_delete(name: str) -> None:
    keyring = _keyring()
    if keyring is None:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except Exception:  # noqa: BLE001 - deleting a missing entry is fine
        pass


# ---- local tables -----------------------------------------------------------


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )


def _db_overrides() -> dict[str, str]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT key, value FROM user_settings WHERE key NOT LIKE 'secret:%'"
        ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def _db_secrets() -> dict[str, str]:
    """Keychain fallback rows (only written when no OS keychain exists)."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT key, value FROM user_settings WHERE key LIKE 'secret:%'"
        ).fetchall()
    return {row["key"].removeprefix("secret:"): row["value"] for row in rows}


def _db_set(key: str, value: str) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO user_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, time.time()),
        )


def _db_delete(key: str) -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM user_settings WHERE key = ?", (key,))


# ---- coercion and validation ------------------------------------------------


def _coerce(field: dict[str, Any], value: Any) -> Any:
    """Coerce one incoming value to the field type; raise ValueError on junk."""
    name = field["name"]
    if field["type"] == "bool":
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in ("1", "true", "yes", "on"):
            return True
        if str(value).strip().lower() in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"{name}: expected true or false")
    if field["type"] == "int":
        try:
            number = int(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{name}: expected a whole number") from exc
        return _clamp(field, number, name)
    if field["type"] == "float":
        try:
            number = float(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{name}: expected a number") from exc
        return _clamp(field, number, name)
    text = str(value).strip()[:STR_LIMIT]
    if field["choices"] and text and text.lower() not in field["choices"]:
        raise ValueError(f"{name}: must be one of {', '.join(field['choices'])}")
    return text.lower() if field["choices"] and text else text


def _clamp(field: dict[str, Any], number: float, name: str) -> float:
    lo, hi = field["lo"], field["hi"]
    if lo is not None and number < lo or hi is not None and number > hi:
        raise ValueError(f"{name}: must be between {lo} and {hi}")
    return number


# ---- persistence and live apply --------------------------------------------


def _secret_source(name: str, db_secrets: dict[str, str]) -> str:
    if keychain_get(name):
        return "keychain"
    if name in db_secrets:
        return "db"
    return "env" if getattr(settings, name) else "not set"


def _secret_set(name: str, value: str) -> None:
    if _keyring() is not None:
        keychain_set(name, value)
        _db_delete(f"secret:{name}")
    else:
        _db_set(f"secret:{name}", value)


def _secret_delete(name: str) -> None:
    keychain_delete(name)
    _db_delete(f"secret:{name}")


def apply_overrides() -> None:
    """Push stored overrides onto the settings object (call once at startup,
    after the DB exists and before anything consumes the values)."""
    _init_db()
    db_secrets = _db_secrets()
    for name in SECRET_NAMES:
        stored = keychain_get(name) or db_secrets.get(name)
        if stored:
            setattr(settings, name, stored)
    overrides = _db_overrides()
    for name, raw in overrides.items():
        field = FIELDS.get(name)
        if field is not None:
            setattr(settings, name, _coerce(field, raw))
    logger.info(
        "[settings] overrides applied (%d keys, keychain=%s)",
        len(overrides) + sum(1 for name in SECRET_NAMES if keychain_get(name) or name in db_secrets),
        keychain_available(),
    )


def save(values: dict[str, Any], clear_secrets: list[str]) -> None:
    """Validate, persist, and live-apply one settings update. Blank secrets
    mean unchanged; clear_secrets wipes keychain/DB entries for that secret."""
    errors: list[str] = []
    clean: dict[str, Any] = {}
    secret_values: dict[str, str] = {}
    for name, value in values.items():
        field = FIELDS.get(name)
        if field is None:
            errors.append(f"unknown setting: {name}")
            continue
        if field["secret"] and (value is None or str(value).strip() == ""):
            continue  # blank = unchanged
        try:
            coerced = _coerce(field, value)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if field["secret"]:
            secret_values[name] = str(coerced)
        else:
            clean[name] = coerced
    for name in clear_secrets:
        if FIELDS.get(name, {}).get("secret") is not True:
            errors.append(f"{name} is not a secret setting")
    if errors:
        raise ValueError("; ".join(errors))

    for name, value in clean.items():
        _db_set(name, str(value))
        setattr(settings, name, value)
    for name in clear_secrets:
        _secret_delete(name)
        setattr(settings, name, ENV_DEFAULTS[name])
    for name, value in secret_values.items():
        _secret_set(name, value)
        setattr(settings, name, value)


def reset() -> None:
    """Drop every override and restore the .env values."""
    _init_db()
    with _connect() as connection:
        connection.execute("DELETE FROM user_settings")
    for name in SECRET_NAMES:
        keychain_delete(name)
    for name, value in ENV_DEFAULTS.items():
        setattr(settings, name, value)


def snapshot() -> dict[str, Any]:
    """The settings page payload: full values for non-secrets, source chips
    and a configured flag for secrets (values are never sent back)."""
    db_secrets = _db_secrets()
    groups = []
    for group in FIELD_GROUPS:
        fields = []
        for field in group["fields"]:
            name = field["name"]
            item = {
                "name": name,
                "env": field["env"],
                "label": field["label"],
                "type": field["type"],
                "secret": field["secret"],
                "choices": list(field["choices"] or []),
                "hint": field.get("hint", ""),
                "section": field.get("section", ""),
                "example": field.get("example", ""),
            }
            if field["secret"]:
                item["configured"] = bool(getattr(settings, name))
                item["source"] = _secret_source(name, db_secrets)
            else:
                item["value"] = getattr(settings, name)
                item["default"] = ENV_DEFAULTS[name]
            fields.append(item)
        groups.append({"key": group["key"], "tab": group["tab"], "label": group["label"], "fields": fields})
    return {
        "groups": groups,
        "keychain_available": keychain_available(),
    }
