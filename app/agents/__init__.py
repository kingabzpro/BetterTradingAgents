"""Shared helpers for the agent modules."""

from dataclasses import dataclass

from app.models import AgentResult


@dataclass
class TaskSpec:
    """One agent call's prompt: a description the model answers and the prose
    contract for what a good answer looks like. Mutable description on purpose:
    corrective retries prepend the validation error to it. No output_type is
    set on purpose: the SDK's structured-output path forces a strict
    json_schema request that some OpenAI-compatible providers (e.g.
    api.deepseek.com) reject, so JSON stays a prompt contract parsed by
    workflow.extract_json."""

    description: str
    expected_output: str


def agent_instructions(role: str, goal: str, backstory: str) -> str:
    """System prompt for one agent, keeping the role/goal/backstory split the
    framework used to format for us."""
    return f"You are {role}.\n\n{backstory}\n\nYour goal: {goal}"

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


def analyst_result(name: str, data: dict) -> AgentResult:
    """Result shape shared by the six researchers (signal + confidence)."""
    return AgentResult(
        agent=name,
        signal=pick_signal(data.get("signal")),
        confidence=clamp_conf(data.get("confidence")),
        summary=clip(data.get("summary", ""), 500),
    )


def scored_result(name: str, signal: str, data: dict) -> AgentResult:
    """Result shape shared by bull and bear: the payload is a debate score."""
    return AgentResult(
        agent=name,
        signal=signal,
        confidence=clamp_conf(data.get("score")),
        summary=clip(data.get("summary", ""), 500),
    )


def first_sentence(text: str, max_len: int = 160) -> str:
    text = text.strip()
    for sep in (". ", "! ", "? "):
        if sep in text:
            text = text.split(sep, 1)[0] + sep.strip()
            break
    return text[:max_len]
