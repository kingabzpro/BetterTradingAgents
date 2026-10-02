"""Analysis depth profiles: pick which agents run to trade thoroughness for speed.

Agent counts per profile (bull, bear and the manager always run):
  fast   = technical + news             + bull/bear + manager = 5 agents
  medium = all five researchers         + bull/bear + manager = 8 agents
  expert = all five researchers + the debate judge            = 9 agents
"""

from typing import Any, Literal

Depth = Literal["fast", "medium", "expert"]
DEFAULT_DEPTH: Depth = "medium"

_ALL_RESEARCH = ("technical", "fundamental", "news", "forecast", "sentiment")

DEPTH_PROFILES: dict[str, dict[str, Any]] = {
    "fast": {
        "label": "Fast",
        "research": ("technical", "news"),
        "note": "Technical + news research only, single debate round.",
    },
    "medium": {
        "label": "Medium",
        "research": _ALL_RESEARCH,
        "note": "All five researchers, single debate round.",
    },
    "expert": {
        "label": "Expert",
        "research": _ALL_RESEARCH,
        "judge": True,
        "note": "All five researchers plus the debate judge.",
    },
}


def normalize_depth(value: object) -> Depth:
    """Coerce anything (API input, old DB rows) into a valid depth key."""
    key = str(value or "").strip().lower()
    return key if key in DEPTH_PROFILES else DEFAULT_DEPTH  # type: ignore[return-value]


def depth_profile(depth: object) -> dict[str, Any]:
    return DEPTH_PROFILES[normalize_depth(depth)]
