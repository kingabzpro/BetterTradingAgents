"""User-selected trading outlook shared by the UI, API and agent prompts."""

from typing import Any, Literal

Outlook = Literal["day_trade", "short_term", "long_term"]
DEFAULT_OUTLOOK: Outlook = "short_term"

OUTLOOKS: dict[str, dict[str, str]] = {
    "day_trade": {
        "label": "Day trading",
        "horizon": "intraday to a couple of days",
        "guidance": (
            "The user is day trading. Weight momentum, ATR/volatility, relative "
            "volume and fresh news catalysts heaviest. Fundamentals and the "
            "5-day forecast are background context only. Signals decay within "
            "hours to days, so stale evidence is weak evidence."
        ),
    },
    "short_term": {
        "label": "Short term",
        "horizon": "days to a few weeks (swing)",
        "guidance": (
            "The user is swing trading. Balance technical trend and momentum "
            "against near-term catalysts and news; use fundamentals as a sanity "
            "check on the thesis. The 5-day forecast is directly relevant."
        ),
    },
    "long_term": {
        "label": "Long term",
        "horizon": "six months to multi-year",
        "guidance": (
            "The user invests for the long term. Weight fundamentals - growth, "
            "margins, valuation and balance-sheet strength - and durable trends "
            "heaviest. Daily technicals and the 5-day forecast are timing aids "
            "only, not the thesis."
        ),
    },
}


def normalize_outlook(value: object) -> Outlook:
    """Coerce anything (API input, old DB rows) into a valid outlook key."""
    key = str(value or "").strip().lower()
    return key if key in OUTLOOKS else DEFAULT_OUTLOOK  # type: ignore[return-value]


def user_context(outlook: object) -> dict[str, Any]:
    """Block injected into every agent payload so all agents share the horizon."""
    key = normalize_outlook(outlook)
    return {
        "outlook": key,
        "label": OUTLOOKS[key]["label"],
        "horizon": OUTLOOKS[key]["horizon"],
        "how_to_weigh_evidence": OUTLOOKS[key]["guidance"],
    }
