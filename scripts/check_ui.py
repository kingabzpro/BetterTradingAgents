"""Offline checks for the shared UI layer (one source for nav and helpers).

Run: PYTHONPATH=. uv run python scripts/check_ui.py
No network: pages render through the real app (TestClient), everything else is
static-file inspection. Guards the drift this pass removed: local helper
copies in page scripts, hand-copied nav markup, and manual ?v= stamps.
"""

import os
import re
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "ui_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["BAKO_API_KEY"] = ""
os.environ["AUTOMATION_ENABLED"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "static"

# Helpers ui.js owns. util.js re-exports them for the analysis modules, and
# report.js builds a standalone offline document with its own print badge.
SHARED = (
    "escapeHtml", "escapeAttr", "showToast", "getClientId",
    "decisionBadge", "evidenceLabel", "fmtCostUsd",
)
EXEMPT = {"ui.js", "util.js", "report.js"}

# ---- one definition site per helper -------------------------------------------
for path in sorted((STATIC / "js").glob("*.js")):
    if path.name in EXEMPT:
        continue
    text = path.read_text(encoding="utf-8")
    for name in SHARED:
        assert not re.search(rf"^function {name}\(", text, re.M), \
            f"static/js/{path.name} defines {name} locally; use window.BTA_UI"
        assert not re.search(rf"^const {name} = (?!window\.BTA_UI)", text, re.M), \
            f"static/js/{path.name} re-declares {name} locally; use window.BTA_UI"
print("single-source helpers OK:", len(SHARED), "helpers, page scripts clean")

# ---- pages carry the nav marker, not hand-copied markup ------------------------
# login.html is exempt on purpose: behind the owner gate (P2.2) it is the one
# page a stranger sees, so it carries no nav and no app surface.
NO_NAV_PAGES = {"login.html"}
for page in sorted(STATIC.glob("*.html")):
    if page.name in NO_NAV_PAGES:
        continue
    text = page.read_text(encoding="utf-8")
    assert text.count("<!--@nav-->") == 1, f"{page.name}: exactly one nav marker expected"
    assert 'aria-label="Primary navigation"' not in text, \
        f"{page.name} carries hand-copied nav markup; keep the <!--@nav--> marker"
    assert "/static/js/ui.js" in text, f"{page.name} must load the shared ui.js"
print("nav markers OK:", len(list(STATIC.glob("*.html"))) - len(NO_NAV_PAGES), "pages")

# ---- no manual asset version stamps --------------------------------------------
for folder, pattern in ((STATIC, "*.html"), (STATIC / "js", "*.js"), (STATIC / "css", "*.css")):
    for path in folder.glob(pattern):
        assert "?v=" not in path.read_text(encoding="utf-8"), \
            f"{path.name} carries a manual ?v= stamp; caching is handled by no-cache + ETag"
print("no manual ?v= stamps OK")

# ---- design tokens must resolve: no self-referential definitions ---------------
# A color sweep once rewrote --ok: #6fe0a0 into --ok: var(--ok), silently
# invalidating every var(--ok) user (stroke: none, colors fall back).
for path in sorted((STATIC / "css").glob("*.css")):
    for match in re.finditer(r"(--[\w-]+):\s*var\((--[\w-]+)\)", path.read_text(encoding="utf-8")):
        assert match.group(1) != match.group(2), \
            f"{path.name}: {match.group(1)} references itself and can never resolve"
print("design tokens resolve OK")

# ---- module imports must resolve: every name imported from the shared modules --
# is actually exported there. A de-dup sweep once left compare.js importing
# DEPTH_LABELS from constants.js after the export moved to ui.js, and only the
# browser (not the offline suite) could see the module error.
def _exported_names(text: str) -> set[str]:
    names = set(re.findall(r"export (?:const|function|let) ([$\w]+)", text))
    for block in re.findall(r"export const \{([^}]*)\}", text, re.S):
        names.update(name.strip() for name in block.split(",") if name.strip())
    return names


for module_name in ("constants.js", "util.js"):
    exports = _exported_names((STATIC / "js" / module_name).read_text(encoding="utf-8"))
    pattern = re.compile(r'import\s*\{([^}]*)\}\s*from\s*"\./' + re.escape(module_name) + r'"', re.S)
    for path in sorted((STATIC / "js").glob("*.js")):
        for block in pattern.findall(path.read_text(encoding="utf-8")):
            for name in (n.strip() for n in block.split(",")):
                if name:
                    assert name in exports, \
                        f"static/js/{path.name} imports {name!r} from {module_name}, which does not export it"
print("module imports resolve OK")

# ---- server-rendered nav: links, active state, chips, feature toggles ----------
client = TestClient(app, base_url="http://127.0.0.1")

for path, label in (("/", "Home"), ("/history", "History"), ("/portfolio", "Trades"), ("/settings", "Settings")):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    text = response.text
    assert text.count('aria-label="Primary navigation"') == 1, path
    assert text.count('aria-current="page"') == 1, path
    current = re.search(r'<a [^>]*aria-current="page"[^>]*>(.*?)</a>', text, re.S)
    assert current and label in current.group(1), (path, label)
    nav = re.search(r'<nav aria-label="Primary navigation">(.*?)</nav>', text, re.S).group(1)
    links = re.findall(r'<a href="([^"]+)"', nav)
    assert links == ["/", "/history", "/portfolio", "/settings"], (path, links)
    if path == "/":
        assert "mode-chip" in nav and "incognito-chip" in nav
    else:
        assert "mode-chip" not in text, f"{path}: chips stay home-only"
print("nav links + active state + chips OK")

# Experimental links appear only while their toggles are on, server-side.
settings.experimental_features = True
settings.feature_watchlist = True
try:
    nav = re.search(
        r'<nav aria-label="Primary navigation">(.*?)</nav>', client.get("/").text, re.S
    ).group(1)
    links = re.findall(r'<a href="([^"]+)"', nav)
    assert links == ["/", "/history", "/watchlist", "/portfolio", "/settings"], links
    assert client.get("/watchlist").status_code == 200
    assert client.get("/accuracy").status_code == 404  # still off
finally:
    settings.experimental_features = False
    settings.feature_watchlist = False
nav = re.search(r'<nav aria-label="Primary navigation">(.*?)</nav>', client.get("/").text, re.S).group(1)
assert 'href="/watchlist"' not in nav
assert client.get("/watchlist").status_code == 404
print("feature-gated nav OK: watchlist link + route appear and vanish with the toggle")

print("ALL UI CHECKS PASSED")
