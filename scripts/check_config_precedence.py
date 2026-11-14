"""Offline regression checks for .env > environment > saved settings."""

import gc
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv


class FakeKeyring:
    def __init__(self):
        self.entries = {
            "llm_api_key": "stored-global-key",
            "llm_api_key_manager": "stored-manager-key",
            "llm_api_key_analysts": "stored-analyst-key",
            "finnhub_api_key": "stored-finnhub-key",
        }

    def get_password(self, service, name):
        return self.entries.get(name)


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    env_file = root / ".env"
    env_file.write_text(
        "LLM_BASE_URL=https://file.example/v1\n"
        "LLM_API_KEY=file-global-key\n"
        "LLM_MODEL=file-model\n"
        "MAX_TICKERS=3\n"
        "ALPACA_TRADING_ENABLED=0\n"
        "FINNHUB_API_KEY=\n",
        encoding="utf-8",
    )
    environment = {
        "DB_PATH": str(root / "settings.db"),
        "LLM_BASE_URL": "https://shell.example/v1",
        "LLM_API_KEY": "shell-global-key",
        "LLM_MODEL": "shell-model",
        "LLM_API_KEY_MANAGER": "shell-manager-key",
        "MAX_TICKERS": "9",
        "ALPACA_TRADING_ENABLED": "1",
    }
    # Redirect only file discovery; exercise the real dotenv parser and config.
    with patch.dict(os.environ, environment, clear=True), patch(
        "dotenv.load_dotenv",
        side_effect=lambda _path, **kwargs: load_dotenv(env_file, **kwargs),
    ):
        from app import settings_store
        from app.config import settings
        from scripts.setup_wizard import key_from

        keyring = FakeKeyring()
        settings_store._keyring_mod = keyring
        assert settings.llm_api_key == "file-global-key", ".env must beat the shell"
        assert settings.llm_base_url == "https://file.example/v1"
        assert settings.llm_model == "file-model"
        assert settings.max_tickers == 3
        assert settings.alpaca_trading_enabled is False
        print("PASS .env overrides shell values, including false/zero")

        settings_store._init_db()
        settings_store._db_set("llm_model", "stored-model")
        settings_store._db_set("max_tickers", "8")
        settings_store._db_set("debate_rounds", "3")
        settings_store._db_set("secret:llm_api_key", "db-global-key")
        settings_store.apply_overrides()
        assert settings.llm_api_key == "file-global-key", "keychain must not override .env"
        assert settings.llm_model == "file-model"
        assert settings.max_tickers == 3
        assert settings.llm_api_key_manager == "shell-manager-key"
        assert settings.llm_api_key_analysts == "stored-analyst-key"
        assert settings.finnhub_api_key == "stored-finnhub-key"
        assert settings.debate_rounds == 3
        assert key_from("llm_api_key") == "file-global-key"
        print("PASS startup and setup use file, shell, then saved fallbacks")

        snapshot = settings_store.snapshot()
        fields = {f["name"]: f for g in snapshot["groups"] for f in g["fields"]}
        assert fields["llm_api_key"]["source"] == "env"
        assert fields["llm_api_key_manager"]["source"] == "env"
        assert fields["llm_api_key_analysts"]["source"] == "keychain"
        assert fields["llm_model"]["locked"] is True
        assert "file-global-key" not in str(snapshot)
        print("PASS Settings reports effective sources without leaking secrets")

        for values, clears in (
            ({"llm_api_key": "replacement-key"}, []),
            ({"llm_model": "replacement-model"}, []),
            ({"llm_api_key_manager": "replacement-manager-key"}, []),
            ({}, ["llm_api_key"]),
        ):
            try:
                settings_store.save(values, clears)
            except ValueError as exc:
                assert "environment" in str(exc)
            else:
                raise AssertionError("Settings must not override an explicit file/shell value")
        assert settings.llm_api_key == "file-global-key"
        assert settings.llm_model == "file-model"
        settings_store.save({"debate_rounds": 2}, [])
        assert settings.debate_rounds == 2
        print("PASS live saves preserve precedence and allow unlocked fields")

    gc.collect()  # SQLite context managers commit but leave handles for GC.

print("Configuration precedence checks passed")
