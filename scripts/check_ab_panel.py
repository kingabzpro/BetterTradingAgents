"""Offline checks for the A/B regression panel (phase 4, item 15).

Pure ledger and comparison math: flip counting, upsert-by-name, same-config
detection, and the rendered table (DIFF markers, noise band, token rows).
No LLM, no network, no backtest run.

Run: uv run python -m scripts.check_ab_panel
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "ab_panel_test.db")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ab_panel  # noqa: E402

TMP = Path(tempfile.mkdtemp())
LEDGER = TMP / "ab-ledger.json"


def entry(name, decisions, votes=1, temperature=0.2, tokens=1000, cost=0.01):
    return {
        "name": name, "created_at": "2026-10-26T00:00:00+00:00", "mode": "llm",
        "depth": "pro", "votes": votes, "temperature": temperature, "model": "m",
        "tickers": "NVDA,AMD,MSFT", "start": "2025-06-02", "end": "2025-07-14",
        "tokens": tokens, "cost_usd": cost, "decisions": decisions,
    }


# ---- flip counting -----------------------------------------------------------
a = {"NVDA@1": "BUY", "AMD@1": "SELL", "MSFT@1": "HOLD"}
b = {"NVDA@1": "BUY", "AMD@1": "HOLD", "MSFT@1": "HOLD"}
diffs = ab_panel.flips(a, b)
assert diffs == [("AMD@1", "SELL", "HOLD")], diffs
assert ab_panel.flips(a, dict(a)) == []
# missing keys count as differences, never as silent agreement
missing = ab_panel.flips({"X@1": "BUY"}, {})
assert missing == [("X@1", "BUY", "-")], missing
print("flip counting OK")

# ---- ledger upsert by name ---------------------------------------------------
ab_panel.save_entry(entry("arm-a", a), LEDGER)
ab_panel.save_entry(entry("arm-b", b), LEDGER)
ab_panel.save_entry(entry("arm-a", a, tokens=2000), LEDGER)  # rerun replaces
entries = ab_panel.load_ledger(LEDGER)
assert len(entries) == 2, entries
assert next(e for e in entries if e["name"] == "arm-a")["tokens"] == 2000
print("ledger upsert OK:", [e["name"] for e in entries])

# ---- same-config detection ---------------------------------------------------
assert ab_panel.same_config(entry("x", a), entry("y", a))
assert not ab_panel.same_config(entry("x", a), entry("y", a, votes=3))
assert not ab_panel.same_config(entry("x", a), entry("y", a, temperature=0.0))
print("same-config detection OK")

# ---- rendered comparison -----------------------------------------------------
text = ab_panel.render_comparison(
    entry("arm-a", a), entry("arm-b", b), control=entry("arm-a-control", dict(a))
)
assert "AMD@1 | SELL | HOLD | DIFF" in text, text
assert "Decision flips: **1/3**" in text, text
assert "Control (arm-a-control" in text and "0/3 flips" in text, text
assert "| arm-a | 1,000 | $0.01 |" in text, text
print("rendered comparison OK")

# ---- CLI surface -------------------------------------------------------------
assert ab_panel.main(["compare", "--a", "missing", "--b", "arm-b"]) == 2  # unknown key exits 2
print("CLI guard OK")

# ---- missing ledger file reads as empty --------------------------------------
assert ab_panel.load_ledger(TMP / "does-not-exist.json") == []
print("AB PANEL CHECKS PASSED")
