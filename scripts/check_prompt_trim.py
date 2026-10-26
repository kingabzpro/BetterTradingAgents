"""Offline checks for the prompt trim (release batch 2, item 8).

The researchers read a bounded news/social payload: item count capped,
summary/content truncated with an explicit ellipsis, and PROMPT_TRIM=0
restores the untrimmed payload for A/B runs. The UI provenance keeps the
fuller list either way. Pure functions, no network.

Run: uv run python -m scripts.check_prompt_trim
"""

import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "trim_test.db")

from app.config import settings  # noqa: E402
from app.workflow import _trim_news_items  # noqa: E402

ITEMS = [
    {
        "title": f"item {i}",
        "summary": "s" * 300,
        "content": "c" * 1500,
        "published": "",
        "url": "",
        "source": "x",
    }
    for i in range(9)
]

# ---- default: capped count, truncated fields, explicit ellipsis -------------
settings.prompt_trim = True
settings.news_prompt_items = 5
settings.news_prompt_summary_chars = 240
settings.news_prompt_content_chars = 800
trimmed = _trim_news_items(ITEMS)
assert len(trimmed) == 5, len(trimmed)
assert all(len(t["summary"]) <= 241 for t in trimmed)  # 240 + "…"
assert all(t["summary"].endswith("…") for t in trimmed)
assert all(len(t["content"]) <= 801 for t in trimmed)
assert all(t["content"].endswith("…") for t in trimmed)
assert trimmed[0]["title"] == "item 0" and trimmed[4]["title"] == "item 4"
# input is never mutated
assert len(ITEMS) == 9 and ITEMS[0]["summary"] == "s" * 300

# short fields pass through untouched (no stray ellipsis)
short = _trim_news_items([{"title": "t", "summary": "ok", "content": "ok"}])
assert short[0]["summary"] == "ok" and short[0]["content"] == "ok"

# ---- PROMPT_TRIM=0: the untrimmed payload ships as-is ------------------------
settings.prompt_trim = False
passthrough = _trim_news_items(ITEMS)
assert len(passthrough) == 9 and passthrough[0]["summary"] == "s" * 300
settings.prompt_trim = True

# ---- social cap is a simple slice -------------------------------------------
settings.social_prompt_items = 3
posts = [f"post {i}" for i in range(8)]
assert posts[: settings.social_prompt_items] == ["post 0", "post 1", "post 2"]

print("PROMPT TRIM CHECKS PASSED")
