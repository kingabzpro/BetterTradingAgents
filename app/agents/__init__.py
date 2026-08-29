"""Shared helpers for the agent modules."""

VALID_SIGNALS = {
    "bullish",
    "bearish",
    "neutral",
    "positive",
    "negative",
    "unknown",
}


def pick_signal(value: object, default: str = "neutral") -> str:
    """Coerce an LLM-provided signal into a known value."""
    if isinstance(value, str):
        cleaned = value.strip().lower().replace("-", " ")
        if cleaned in VALID_SIGNALS:
            return cleaned
    return default


def clamp_conf(value: object, default: float = 0.5) -> float:
    """Coerce a confidence/score into 0.0-1.0 (handles 0-100 scales)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if 1.0 < number <= 100.0:
        number /= 100.0
    return min(max(number, 0.0), 1.0)


def clip(text: str, limit: int) -> str:
    """Truncate to a limit, marking cuts with an ellipsis."""
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def first_sentence(text: str, max_len: int = 160) -> str:
    text = text.strip()
    for sep in (". ", "! ", "? "):
        if sep in text:
            text = text.split(sep, 1)[0] + sep.strip()
            break
    return text[:max_len]
