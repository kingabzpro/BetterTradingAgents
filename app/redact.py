"""Keep configured secret values out of logs, events, history, and errors.

Provider exceptions can carry request context, and for Finnhub and Tavily the
API key rides in the URL query string, so raw exception text must never flow
into a logger, an SSE payload, a persisted run, or an HTTP error detail
unchecked (P2.23, OWASP LLM02). settings_store.SECRET_NAMES is the single
source of truth for what counts as a secret, so new key fields are covered
automatically.
"""

from app import settings_store
from app.config import settings


def _secret_values() -> list[str]:
    """Every configured secret value long enough to match without noise.

    Short values (under 8 chars) are unusable as needles and would redact
    ordinary text around them, so they are skipped. Read live each call:
    keys can change through Settings without a restart.
    """
    values = [getattr(settings, name, "") or "" for name in settings_store.SECRET_NAMES]
    return [value for value in values if len(value) >= 8]


def redact(text: str) -> str:
    """Replace every configured secret value in text with [redacted]."""
    for value in _secret_values():
        if value in text:
            text = text.replace(value, "[redacted]")
    return text
