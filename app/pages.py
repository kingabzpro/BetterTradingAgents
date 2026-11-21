"""Shared page shell: one source for the primary navigation.

Pages carry a `<!--@nav-->` marker where the nav goes; `render_page` fills it
from one template so the seven pages cannot drift apart, marks the current
page, and includes the experimental links only while their feature toggles
are on (same rules the client-side normalizer in static/js/features.js
applies, which stays as a no-op belt-and-braces pass).

Asset freshness is handled by the no-cache + ETag middleware in app/main.py,
so pages carry no manual ?v= version stamps anymore.
"""

import logging
from pathlib import Path

from fastapi.responses import HTMLResponse

logger = logging.getLogger("pages")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

NAV_MARKER = "<!--@nav-->"

# Experimental pages, in nav order, keyed by the toggle in app/config.py.
FEATURE_LINKS = (
    ("accuracy", "Accuracy", "/accuracy"),
    ("compare", "Compare", "/compare"),
    ("watchlist", "Watchlist", "/watchlist"),
)

_GEAR_SVG = (
    '<svg class="gear" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/>'
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'
)


def render_nav(active: str, features: dict[str, bool]) -> str:
    """The primary nav for one page: chips (home only) + links, current marked."""
    parts: list[str] = []
    if active == "home":
        parts.append(
            '<span id="mode-chip" class="chip hidden" aria-label="Mock mode: agents use rule-based fallbacks">mock mode</span>'
        )
        parts.append(
            '<span id="incognito-chip" class="chip incognito hidden" aria-label="Incognito mode on: market-only analysis, nothing saved">incognito</span>'
        )
    links = [("home", "Home", "/"), ("history", "History", "/history"), ("backtests", "Backtests", "/backtests")]
    links += [
        (key, label, href) for key, label, href in FEATURE_LINKS if features.get(key)
    ]
    links += [("trades", "Trades", "/portfolio"), ("settings", "Settings", "/settings")]
    for key, label, href in links:
        classes = "nav-link" + (" nav-settings" if key == "settings" else "")
        current = ""
        if key == active:
            classes += " active"
            current = ' aria-current="page"'
        inner = f"{_GEAR_SVG}<span>Settings</span>" if key == "settings" else label
        parts.append(f'<a href="{href}" class="{classes}"{current}>{inner}</a>')
    body = "\n        ".join(parts)
    return f'<nav aria-label="Primary navigation">\n        {body}\n      </nav>'


def render_page(filename: str, active: str, features: dict[str, bool]) -> HTMLResponse:
    """Serve one static page with the shared nav filled in."""
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")
    if html.count(NAV_MARKER) != 1:
        logger.error("[pages] %s must contain exactly one %s marker", filename, NAV_MARKER)
        raise RuntimeError(f"{filename} is missing its {NAV_MARKER} marker")
    return HTMLResponse(html.replace(NAV_MARKER, render_nav(active, features)))
