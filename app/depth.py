"""Analysis depth profiles: pick which agents run to trade thoroughness for speed.

Agent counts per profile:
  fast = technical + forecast + manager                           = 3 agents (super fast;
          no debate and no news fetch: the manager weighs the research directly)
  pro  = market + technical + news + forecast + bull/bear + manager = 7 agents
  max  = all six researchers + judge + bull/bear + manager = 10 agents

Pro's four researchers mirror the source framework's analyst team
(fundamentals/sentiment/news/technical in TradingAgents, arXiv 2412.20138)
adapted for short horizons: regime, price action, catalysts, projection.
Fundamentals and sentiment are the long-horizon and noisiest inputs, so they
differentiate Max. The old tier names ("medium", "expert") are aliased so
runs and links saved before the rename keep working.
"""

from typing import Any, Literal

Depth = Literal["fast", "pro", "max"]
DEFAULT_DEPTH: Depth = "pro"
_DEPTH_ALIASES = {"medium": "pro", "expert": "max"}

_ALL_RESEARCH = ("market", "technical", "fundamental", "news", "forecast", "sentiment")

DEPTH_PROFILES: dict[str, dict[str, Any]] = {
    "fast": {
        "label": "Fast",
        "research": ("technical", "forecast"),
        "debate": False,
        "note": "Super fast scan: price action and projection, weighed directly by the manager.",
    },
    "pro": {
        "label": "Pro",
        "research": ("market", "technical", "news", "forecast"),
        "note": "Regime, price action, catalysts and projection, single debate round.",
    },
    "max": {
        "label": "Max",
        "research": _ALL_RESEARCH,
        "judge": True,
        "note": "All six researchers plus the debate judge.",
    },
}


def normalize_depth(value: object) -> Depth:
    """Coerce anything (API input, old DB rows) into a valid depth key."""
    key = str(value or "").strip().lower()
    key = _DEPTH_ALIASES.get(key, key)
    return key if key in DEPTH_PROFILES else DEFAULT_DEPTH  # type: ignore[return-value]


def depth_profile(depth: object) -> dict[str, Any]:
    return DEPTH_PROFILES[normalize_depth(depth)]
