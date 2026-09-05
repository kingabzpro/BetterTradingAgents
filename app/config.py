"""Application configuration loaded from environment variables / .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Must happen before crewai is imported anywhere.
os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Settings:
    """Simple env-driven settings. No hierarchy, no layers."""

    # LLM (any OpenAI-compatible endpoint: OpenAI, OpenRouter, DeepSeek, Qwen, GLM, vLLM...)
    llm_base_url: str = _env("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key: str = _env("LLM_API_KEY")
    llm_model: str = _env("LLM_MODEL", "gpt-4o-mini")
    llm_temperature: float = float(_env("LLM_TEMPERATURE", "0.2"))
    llm_timeout_seconds: float = float(_env("LLM_TIMEOUT_SECONDS", "90"))
    # Optional provider-specific reasoning effort (e.g. "none"/"low" for GLM).
    llm_reasoning_effort: str = _env("LLM_REASONING_EFFORT")
    # Per-role overrides (docs/ROADMAP.md 2.3); each falls back to the global
    # LLM_* value. Roles: analysts = the 4 researchers, debate = bull/bear,
    # manager = the final BUY/HOLD/SELL call - cheap fast researchers, a
    # stronger model only where judgment matters.
    llm_model_manager: str = _env("LLM_MODEL_MANAGER")
    llm_base_url_manager: str = _env("LLM_BASE_URL_MANAGER")
    llm_api_key_manager: str = _env("LLM_API_KEY_MANAGER")
    llm_model_analysts: str = _env("LLM_MODEL_ANALYSTS")
    llm_base_url_analysts: str = _env("LLM_BASE_URL_ANALYSTS")
    llm_api_key_analysts: str = _env("LLM_API_KEY_ANALYSTS")
    llm_model_debate: str = _env("LLM_MODEL_DEBATE")
    llm_base_url_debate: str = _env("LLM_BASE_URL_DEBATE")
    llm_api_key_debate: str = _env("LLM_API_KEY_DEBATE")

    # Data providers
    finnhub_api_key: str = _env("FINNHUB_API_KEY")
    olostep_api_key: str = _env("OLOSTEP_API_KEY")
    nixtla_api_key: str = _env("NIXTLA_API_KEY")

    # Analysis limits
    max_tickers: int = int(_env("MAX_TICKERS", "5"))
    # 1 = single round; >= 2 adds one bull/bear rebuttal exchange (capped at 3).
    debate_rounds: int = min(3, max(1, int(_env("DEBATE_ROUNDS", "2"))))

    # Demo portfolio
    starting_cash: float = float(_env("STARTING_CASH", "100000"))
    default_position_size: float = float(_env("DEFAULT_POSITION_SIZE", "10000"))
    db_path: Path = Path(_env("DB_PATH", str(BASE_DIR / "portfolio.db")))

    # Risk gate (docs/ROADMAP.md 1.2) - fractions of total equity
    max_position_pct: float = float(_env("MAX_POSITION_PCT", "0.10"))
    max_invested_pct: float = float(_env("MAX_INVESTED_PCT", "0.60"))
    min_cash_pct: float = float(_env("MIN_CASH_PCT", "0.10"))

    # Decision memory (docs/ROADMAP.md 1.1): days a decision is held before its
    # outcome is final; LLM-written reflections are opt-in (off = deterministic).
    memory_horizon_days: int = max(1, int(_env("MEMORY_HORIZON_DAYS", "21")))
    memory_reflect_with_llm: bool = _env("MEMORY_REFLECT_WITH_LLM", "0").lower() in (
        "1",
        "true",
        "yes",
    )

    # Live reasoning stream (docs/ROADMAP.md 3.2): stream agent tokens to the
    # UI as agent_token SSE events. A provider that rejects streaming makes the
    # role's LLM fall back to non-streaming on first use (like the JSON-mode drop).
    stream_reasoning: bool = _env("STREAM_REASONING", "1").lower() in ("1", "true", "yes")

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    def llm_for(self, role: str) -> dict:
        """Resolved {model, base_url, api_key} for one role
        (manager / analysts / debate); per-role overrides fall back to LLM_*."""
        return {
            "model": getattr(self, f"llm_model_{role}", "") or self.llm_model,
            "base_url": getattr(self, f"llm_base_url_{role}", "") or self.llm_base_url,
            "api_key": getattr(self, f"llm_api_key_{role}", "") or self.llm_api_key,
        }


settings = Settings()
