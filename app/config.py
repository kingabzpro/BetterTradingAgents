"""Application configuration loaded from environment variables / .env file."""

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# Must happen before the agents SDK is imported anywhere: keeps the tracer
# from ever warning about or exporting to an absent OPENAI_API_KEY.
os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# P2.11: the LLM API key follows base_url to whatever host it names, so a
# plain-http remote URL would hand the key to anyone on the path. Local
# development endpoints (vLLM, Ollama, LiteLLM...) stay allowed.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def safe_base_url(url: str) -> bool:
    """True when base_url cannot leak the API key: https anywhere, or http
    only to a loopback host. Anything else (including bare hostnames with
    no scheme) is rejected wherever the value enters."""
    parsed = urlparse(url.strip())
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS


# Which role each agent runs as - the single source for the workflow's LLM
# keying and for the agent -> role -> global LLM config resolution below.
# Agents with their own settings fields (the six researchers + judge) can
# override their role; bull/bear ride the debate role.
AGENT_ROLES = {
    "market": "analysts",
    "technical": "analysts",
    "fundamental": "analysts",
    "news": "analysts",
    "sentiment": "analysts",
    "forecast": "analysts",
    "bull": "debate",
    "bear": "debate",
    "judge": "debate",
    "manager": "manager",
}
# Agents that carry per-agent llm_* settings fields.
AGENT_LLM_KEYS = ("market", "technical", "fundamental", "news", "sentiment", "forecast", "judge")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Pure LLM defaults, importable before the .env layer resolves (the setup
# wizard uses them to tell "explicitly configured" from untouched defaults).
LLM_DEFAULT_BASE_URL = "https://api.openai.com/v1"
LLM_DEFAULT_MODEL = "gpt-6-luna"


class Settings:
    """Simple env-driven settings. No hierarchy, no layers."""

    # LLM (any OpenAI-compatible endpoint: OpenAI, OpenRouter, DeepSeek, Qwen, GLM, vLLM...)
    llm_base_url: str = _env("LLM_BASE_URL", LLM_DEFAULT_BASE_URL)
    llm_api_key: str = _env("LLM_API_KEY")
    llm_model: str = _env("LLM_MODEL", LLM_DEFAULT_MODEL)
    # 0.0 default since the phase-4 panel: identical runs flipped 6/20 decisions
    # at 0.2 vs 2/20 at 0.0 (pooled 31% -> 14% over 29 pairs/arm) and 0.0 also
    # costs ~4.6% less. Any explicit LLM_TEMPERATURE (env or .env) still wins.
    llm_temperature: float = float(_env("LLM_TEMPERATURE", "0.0"))
    llm_timeout_seconds: float = float(_env("LLM_TIMEOUT_SECONDS", "90"))
    # P2.18 runaway bounds: max tokens forwarded to every provider (0 = let
    # the provider default stand) and smart retries per LLM call after
    # 429/5xx/malformed-output failures (0 = fail fast). Defaults = today.
    llm_max_tokens: int = max(0, int(_env("MAX_TOKENS", "0")))
    llm_max_retries: int = min(10, max(0, int(_env("LLM_MAX_RETRIES", "2"))))
    # Cap on in-flight LLM calls across the whole app. Parallel researchers +
    # multi-ticker runs can burst past what shared endpoints allow (Modal's
    # shared DeepSeek endpoints cap at 16 concurrent per workspace), and every
    # bounced call costs a retry round-trip. 0 = unlimited (default off).
    llm_concurrency: int = max(0, int(_env("LLM_CONCURRENCY", "12")))
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
    # Per-agent overrides (same three knobs per agent name); resolution is
    # agent -> role -> global, so a lone global LLM_* still covers everyone.
    llm_model_market: str = _env("LLM_MODEL_MARKET")
    llm_base_url_market: str = _env("LLM_BASE_URL_MARKET")
    llm_api_key_market: str = _env("LLM_API_KEY_MARKET")
    llm_model_technical: str = _env("LLM_MODEL_TECHNICAL")
    llm_base_url_technical: str = _env("LLM_BASE_URL_TECHNICAL")
    llm_api_key_technical: str = _env("LLM_API_KEY_TECHNICAL")
    llm_model_fundamental: str = _env("LLM_MODEL_FUNDAMENTAL")
    llm_base_url_fundamental: str = _env("LLM_BASE_URL_FUNDAMENTAL")
    llm_api_key_fundamental: str = _env("LLM_API_KEY_FUNDAMENTAL")
    llm_model_news: str = _env("LLM_MODEL_NEWS")
    llm_base_url_news: str = _env("LLM_BASE_URL_NEWS")
    llm_api_key_news: str = _env("LLM_API_KEY_NEWS")
    llm_model_sentiment: str = _env("LLM_MODEL_SENTIMENT")
    llm_base_url_sentiment: str = _env("LLM_BASE_URL_SENTIMENT")
    llm_api_key_sentiment: str = _env("LLM_API_KEY_SENTIMENT")
    llm_model_forecast: str = _env("LLM_MODEL_FORECAST")
    llm_base_url_forecast: str = _env("LLM_BASE_URL_FORECAST")
    llm_api_key_forecast: str = _env("LLM_API_KEY_FORECAST")
    llm_model_judge: str = _env("LLM_MODEL_JUDGE")
    llm_base_url_judge: str = _env("LLM_BASE_URL_JUDGE")
    llm_api_key_judge: str = _env("LLM_API_KEY_JUDGE")
    # Cost estimate overrides (app/cost.py): USD per 1M input/output tokens
    # applied to every role regardless of model - for custom or proxied
    # pricing. 0 = use the built-in list-price table (app/cost.py).
    llm_price_in: float = float(_env("LLM_PRICE_IN", "0") or 0)
    llm_price_out: float = float(_env("LLM_PRICE_OUT", "0") or 0)

    # Data providers
    finnhub_api_key: str = _env("FINNHUB_API_KEY")
    olostep_api_key: str = _env("OLOSTEP_API_KEY")
    # Web-search fallbacks behind Olostep for the news chain (search only).
    tavily_api_key: str = _env("TAVILY_API_KEY")
    firecrawl_api_key: str = _env("FIRECRAWL_API_KEY")
    # Bako serves per-symbol ML price predictions (TimesFM via Vertex AI);
    # the hosted forecast in the chain (Bako, then the local trend model on
    # the yfinance closes). Free plan: 100 calls/day.
    bako_api_key: str = _env("BAKO_API_KEY")
    # SEC EDGAR (ROADMAP P1.15) needs no key, but asks callers to identify
    # themselves. Set your own contact so SEC can reach you, not the project.
    sec_edgar_user_agent: str = _env("SEC_EDGAR_USER_AGENT")

    # Analysis limits
    max_tickers: int = int(_env("MAX_TICKERS", "5"))
    # 1 = bull/bear only; >= 2 adds the debate judge cross-examination (capped at 3).
    debate_rounds: int = min(3, max(1, int(_env("DEBATE_ROUNDS", "2"))))
    # Wall-clock deadline for one analysis run (P2.8): a provider that hangs
    # must fail the run instead of leaving it "running" forever.
    run_timeout_minutes: int = max(5, int(_env("RUN_TIMEOUT_MINUTES", "30")))

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
    # Scheduled settling (wiki Roadmap P1.17): every interval, decisions whose
    # horizon window closed are graded even if their ticker is never analyzed
    # again, so the scorecard fills in on its own.
    memory_settle_interval_minutes: int = max(
        15, int(_env("MEMORY_SETTLE_INTERVAL_MINUTES", "60"))
    )
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
    # Calibration-driven sizing (phase 4, item 19): scale proposed BUY risk by
    # the manager's own reliability curve (observed/predicted per probability
    # band, clamped 0.5-1.0, never inflating). Live runs only; replays stay
    # out of the live record. Off by default.
    calibration_sizing_enabled: bool = _env("CALIBRATION_SIZING", "0").lower() in (
        "1",
        "true",
        "yes",
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
    # Terminal log trail: 0 = warnings and errors only (quiet terminal),
    # 1 = the full INFO trail plus uvicorn request lines.
    terminal_logging: bool = _env("TERMINAL_LOGGING", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    # Server port for `uv run app`; when busy, the next free port is used.
    app_port: int = min(max(int(_env("APP_PORT", "8000")), 1), 65535)
    alpaca_max_order_usd: float = float(_env("ALPACA_MAX_ORDER_USD", "10000"))
    alpaca_max_orders_per_day: int = int(_env("ALPACA_MAX_ORDERS_PER_DAY", "20"))
    alpaca_max_decision_age_hours: float = float(
        _env("ALPACA_MAX_DECISION_AGE_HOURS", "72")
    )
    # Broker reconciliation (wiki Roadmap P2.3): a startup sweep plus an
    # interval sweep refresh non-terminal local orders by client order id and
    # diff Alpaca's open orders against the local set (the out-of-sync banner).
    broker_sync_interval_minutes: int = max(
        5, int(_env("BROKER_SYNC_INTERVAL_MINUTES", "15"))
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
    # The discovery screen is deterministic, so without a cooldown every
    # session re-runs the same top names (and re-analyzing a held name is
    # wasted work: autopilot never adds to a position). Candidates analyzed
    # by sessions in the last N days are rotated out of the shortlist; they
    # backfill once the fresh pool runs short. 0 disables the rotation.
    automation_candidate_cooldown_days: int = max(
        0, int(_env("AUTOMATION_CANDIDATE_COOLDOWN_DAYS", "3"))
    )
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
    # Portfolio budget (wiki Roadmap P2.4): autopilot BUYs stop once the
    # notional autopilot has deployed (its non-rejected buys minus its filled
    # sells) passes this share of equity, and never take cash below the floor
    # share of equity. 1.0 / 0.0 = today: only the global risk gate applies.
    automation_max_deployed_pct: float = float(
        _env("AUTOMATION_MAX_DEPLOYED_PCT", "1.0")
    )
    automation_cash_floor_pct: float = max(
        0.0, float(_env("AUTOMATION_CASH_FLOOR_PCT", "0.0"))
    )
    # One plain-text POST to this URL per finished autopilot session
    # (completed or failed); empty = off. Best-effort: a webhook failure
    # never fails the session it reports on.
    webhook_url: str = _env("WEBHOOK_URL")

    # Live reasoning stream (wiki Roadmap 3.2): stream agent tokens to the
    # UI as agent_token SSE events. OFF by default - in live use the stream is
    # mostly the final JSON blob, which reads as noise next to the result card.
    # Set STREAM_REASONING=1 to enable; a provider that rejects streaming makes
    # the role's LLM fall back to non-streaming on first use.
    stream_reasoning: bool = _env("STREAM_REASONING", "0").lower() in ("1", "true", "yes")

    # Prompt trim (release batch 2, item 8): cap the news/social payloads the
    # researchers read (NOT what the UI shows; provenance keeps the full
    # list). PROMPT_TRIM=0 restores the untrimmed payloads for A/B runs.
    prompt_trim: bool = _env("PROMPT_TRIM", "1").lower() in ("1", "true", "yes")
    news_prompt_items: int = max(1, int(_env("NEWS_PROMPT_ITEMS", "5")))
    news_prompt_summary_chars: int = max(80, int(_env("NEWS_PROMPT_SUMMARY_CHARS", "240")))
    news_prompt_content_chars: int = max(200, int(_env("NEWS_PROMPT_CONTENT_CHARS", "800")))
    social_prompt_items: int = max(1, int(_env("SOCIAL_PROMPT_ITEMS", "8")))

    # Live data TTL cache (release batch 2, item 9): news and history fetches
    # are reused for this many seconds across analyses of the same ticker in
    # one process, so a watchlist rerun does not re-pay every provider.
    # 0 disables the cache (every analysis fetches fresh).
    news_cache_ttl_seconds: int = max(0, int(_env("NEWS_CACHE_TTL_SECONDS", "900")))
    history_cache_ttl_seconds: int = max(0, int(_env("HISTORY_CACHE_TTL_SECONDS", "300")))

    # Pre-trade turnover guard (release batch 3, item 12): one autopilot
    # session's proposed orders (buys + sells) may not exceed this share of
    # equity; when over, every order is scaled down proportionally (the
    # FinRL-X pattern) instead of rejected. 1.0 = off.
    max_turnover_pct: float = min(1.0, max(0.01, float(_env("MAX_TURNOVER_PCT", "0.5"))))

    # Risk overlay (release batch 3, item 13): deterministic exits between
    # agent runs - an absolute stop from entry, a trailing stop from the
    # position's peak, and a cooldown that blocks re-entry after a stop. OFF
    # by default so autopilot behavior never changes silently; flip it on the
    # Settings page. The overlay only ever SELLS (paper exits), never buys.
    overlay_enabled: bool = _env("OVERLAY_ENABLED", "0").lower() in ("1", "true", "yes")
    overlay_stop_loss_pct: float = min(
        1.0, max(0.01, float(_env("OVERLAY_STOP_LOSS_PCT", "0.12")))
    )
    overlay_trailing_stop_pct: float = min(
        1.0, max(0.01, float(_env("OVERLAY_TRAILING_STOP_PCT", "0.15")))
    )
    overlay_cooldown_days: int = max(0, int(_env("OVERLAY_COOLDOWN_DAYS", "5")))

    # Risk-regime gate (phase 4, item 18; app/risk_regime.py): a deterministic
    # SPY/VIX read that scales the risk budget for NEW BUYs (risk_on 1.0x,
    # neutral 0.75x, risk_off 0.0x with persistence, plus a fast 3-session
    # shock overlay). OFF by default like the overlay, so sizing behavior
    # never changes silently; flip it on the Settings page.
    risk_regime_enabled: bool = _env("RISK_REGIME_ENABLED", "0").lower() in (
        "1",
        "true",
        "yes",
    )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def alpaca_configured(self) -> bool:
        return bool(self.alpaca_api_key_id and self.alpaca_api_secret_key)

    def llm_for(self, key: str) -> dict:
        """Resolved {model, base_url, api_key} for an agent name or a role
        (manager / analysts / debate); per-agent overrides beat the role
        override, which falls back to the global LLM_* values."""
        role = AGENT_ROLES.get(key, key)

        def pick(suffix: str) -> str:
            return (
                getattr(self, f"llm_{suffix}_{key}", "")
                or getattr(self, f"llm_{suffix}_{role}", "")
                or getattr(self, f"llm_{suffix}")
            )

        return {
            "model": pick("model"),
            "base_url": pick("base_url"),
            "api_key": pick("api_key"),
        }

    def llm_key_for(self, agent: str) -> str:
        """LLM cache key for one agent: the agent's own name when it
        overrides its role's endpoint config, else the role (agents without
        an override share the role's LLM object)."""
        overridden = any(
            getattr(self, f"llm_{suffix}_{agent}", "")
            for suffix in ("model", "base_url", "api_key")
        )
        return agent if overridden else AGENT_ROLES.get(agent, agent)


settings = Settings()
