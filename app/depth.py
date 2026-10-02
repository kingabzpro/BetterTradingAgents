"""Analysis depth profiles: pick which agents run to trade thoroughness for speed.

Agent counts per profile (bull, bear and the manager always run):
  fast = technical + news             + bull/bear + manager = 5 agents (super fast)
  pro  = all six researchers          + bull/bear + manager = 9 agents
  max  = all six researchers + judge + bull/bear + manager = 10 agents

The old tier names ("medium", "expert") are aliased so runs and links saved
before the rename keep working.
"""

from typing import Any, Literal

Depth = Literal["fast", "pro", "max"]
DEFAULT_DEPTH: Depth = "pro"
_DEPTH_ALIASES = {"medium": "pro", "expert": "max"}

_ALL_RESEARCH = ("market", "technical", "fundamental", "news", "forecast", "sentiment")

DEPTH_PROFILES: dict[str, dict[str, Any]] = {
    "fast": {
        "label": "Fast",
        "research": ("technical", "news"),
        "note": "Super fast scan: technical + news research, single debate round.",
    },
    "pro": {
        "label": "Pro",
        "research": _ALL_RESEARCH,
        "note": "All six researchers, single debate round.",
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
