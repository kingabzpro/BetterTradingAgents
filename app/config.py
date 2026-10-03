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
    llm_model: str = _env("LLM_MODEL", "gpt-5.6-luna")
    llm_temperature: float = float(_env("LLM_TEMPERATURE", "0.2"))
    llm_timeout_seconds: float = float(_env("LLM_TIMEOUT_SECONDS", "90"))
    # Optional provider-specific reasoning effort (e.g. "none"/"low" for GLM).
    llm_reasoning_effort: str = _env("LLM_REASONING_EFFORT")
    llm_reasoning_effort_manager: str = _env("LLM_REASONING_EFFORT_MANAGER")
    # Per-role overrides (wiki Roadmap 2.3); each falls back to the global
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
    # Cost estimate overrides (app/cost.py): USD per 1M input/output tokens
    # applied to every role regardless of model - for custom or proxied
    # pricing. 0 = use the built-in list-price table (app/cost.py).
    llm_price_in: float = float(_env("LLM_PRICE_IN", "0") or 0)
    llm_price_out: float = float(_env("LLM_PRICE_OUT", "0") or 0)

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

    # Risk gate (wiki Roadmap 1.2) - fractions of total equity
    max_position_pct: float = float(_env("MAX_POSITION_PCT", "0.10"))
    max_invested_pct: float = float(_env("MAX_INVESTED_PCT", "0.60"))
    min_cash_pct: float = float(_env("MIN_CASH_PCT", "0.10"))
    # Drawdown brake: BUYs halt once open positions' unrealized loss exceeds
    # this share of the cash-plus-cost basis behind them (Alpaca's multi-agent
    # example halts at 15% total drawdown).
    max_drawdown_pct: float = float(_env("MAX_DRAWDOWN_PCT", "0.15"))
    # Open-position count cap: a BUY of a new ticker halts at this many open
    # positions; adding to one already held is unaffected.
    max_positions: int = int(_env("MAX_POSITIONS", "10"))
    # Correlated-group cap (wiki Roadmap P1.3): a BUY whose exposure, combined
    # with holdings whose daily returns correlate at/above 0.7 (app/risk.py),
    # would push the group past this share of equity raises a warning. It is
    # warning-only; the caps above still block the trade.
    max_correlated_pct: float = float(_env("MAX_CORRELATED_PCT", "0.25"))

    # Experimental features (retired UIs kept behind an opt-in): the master
    # switch gates the three per-feature toggles; a feature is served only
    # when both are on. Editable live on the Settings page.
    experimental_features: bool = _env("EXPERIMENTAL_FEATURES", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    feature_accuracy: bool = _env("FEATURE_ACCURACY", "0").lower() in ("1", "true", "yes")
    feature_compare: bool = _env("FEATURE_COMPARE", "0").lower() in ("1", "true", "yes")
    feature_watchlist: bool = _env("FEATURE_WATCHLIST", "0").lower() in ("1", "true", "yes")

    # Decision memory (wiki Roadmap 1.1): days a decision is held before its
    # outcome is final; LLM-written reflections are opt-in (off = deterministic).
    memory_horizon_days: int = max(1, int(_env("MEMORY_HORIZON_DAYS", "21")))
    memory_reflect_with_llm: bool = _env("MEMORY_REFLECT_WITH_LLM", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    # Confidence calibration (wiki Roadmap P1.1): a bucket with fewer mature
    # graded decisions than this reports "Track record unavailable" instead of
    # a rate that a handful of outcomes cannot support.
    calibration_min_observations: int = max(
        1, int(_env("CALIBRATION_MIN_OBSERVATIONS", "30"))
    )

    # Alpaca paper trading (wiki Roadmap P2.1). Paper only: app/broker.py
    # constructs the SDK client with paper=True hardcoded, so no configuration
    # can point this feature at the live broker. The feature stays dormant
    # until both key variables exist. Both common spellings are accepted.
    alpaca_api_key_id: str = _env("ALPACA_API_KEY_ID") or _env("ALPACA_API_KEY")
    alpaca_api_secret_key: str = _env("ALPACA_API_SECRET_KEY") or _env(
        "ALPACA_SECRET_KEY"
    )
    # Kill switch: 0 = read-only, no order submissions.
    alpaca_trading_enabled: bool = _env("ALPACA_TRADING_ENABLED", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    alpaca_max_order_usd: float = float(_env("ALPACA_MAX_ORDER_USD", "10000"))
    alpaca_max_orders_per_day: int = int(_env("ALPACA_MAX_ORDERS_PER_DAY", "20"))
    alpaca_max_decision_age_hours: float = float(
        _env("ALPACA_MAX_DECISION_AGE_HOURS", "72")
    )

    # Automated paper-trading sessions (autopilot). A background loop scans
    # the market for candidates (the discovery screen), adds held tickers so
    # SELL decisions can act, runs the normal agent pipeline as one run, and
    # submits the risk-gated trades through broker.submit_order - so the kill
    # switch, order caps, and decision-age checks still apply to every order.
    # Sessions never trade mock decisions and scheduled ones only run while
    # the market is open. The on/off toggle itself lives in the DB (the
    # portfolio page flips it); this env value only seeds the first boot.
    automation_enabled: bool = _env("AUTOMATION_ENABLED", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    automation_interval_minutes: int = max(
        15, int(_env("AUTOMATION_INTERVAL_MINUTES", "240"))
    )
    automation_candidates: int = max(1, int(_env("AUTOMATION_CANDIDATES", "3")))
    # BUYs need at least this final (risk-gated) confidence; SELLs always act
    # because they only exit positions already held.
    automation_min_confidence: float = float(_env("AUTOMATION_MIN_CONFIDENCE", "0.65"))
    automation_outlook: str = _env("AUTOMATION_OUTLOOK", "short_term")
    automation_depth: str = _env("AUTOMATION_DEPTH", "pro")
    automation_allow_sells: bool = _env("AUTOMATION_ALLOW_SELLS", "1").lower() in (
        "1",
        "true",
        "yes",
    )
    # One hung provider must not wedge the loop forever: a session longer
    # than this is cancelled and recorded as failed.
    automation_session_timeout_minutes: int = max(
        5, int(_env("AUTOMATION_SESSION_TIMEOUT_MINUTES", "40"))
    )

    # Live reasoning stream (wiki Roadmap 3.2): stream agent tokens to the
    # UI as agent_token SSE events. OFF by default - in live use the stream is
    # mostly the final JSON blob, which reads as noise next to the result card.
    # Set STREAM_REASONING=1 to enable; a provider that rejects streaming makes
    # the role's LLM fall back to non-streaming on first use.
    stream_reasoning: bool = _env("STREAM_REASONING", "0").lower() in ("1", "true", "yes")

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def alpaca_configured(self) -> bool:
        return bool(self.alpaca_api_key_id and self.alpaca_api_secret_key)

    def llm_for(self, role: str) -> dict:
        """Resolved {model, base_url, api_key} for one role
        (manager / analysts / debate); per-role overrides fall back to LLM_*."""
        return {
            "model": getattr(self, f"llm_model_{role}", "") or self.llm_model,
            "base_url": getattr(self, f"llm_base_url_{role}", "") or self.llm_base_url,
            "api_key": getattr(self, f"llm_api_key_{role}", "") or self.llm_api_key,
        }


settings = Settings()
