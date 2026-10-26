"""Offline checks for manager self-consistency voting (phase 4, item 14).

Pure voting math: unanimous, majority, no-HOLD ties, confidence-weighted
ties, payload selection, malformed samples, and the record fields that carry
the vote to the UI. No LLM and no network.

Run: uv run python -m scripts.check_voting
"""

import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "voting_test.db")
os.environ["MANAGER_VOTES"] = "3"  # parsed at import: proves the env wiring

from app.agents import manager as manager_agent  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import StockAnalysis  # noqa: E402
from app import settings_store  # noqa: E402


def sample(decision: str, confidence: float, summary: str = "") -> dict:
    return {"decision": decision, "confidence": confidence, "summary": summary or decision}


# ---- env wiring and clamp ---------------------------------------------------
assert settings.manager_votes == 3, settings.manager_votes
print("MANAGER_VOTES env wiring OK:", settings.manager_votes)

# ---- unanimous vote ---------------------------------------------------------
payload, votes, agreement = manager_agent.majority_vote(
    [sample("BUY", 0.6), sample("BUY", 0.9, "best"), sample("BUY", 0.7)], "T"
)
assert votes == ["BUY", "BUY", "BUY"] and agreement == 1.0
assert payload["summary"] == "best", payload  # highest-confidence sample wins

# ---- majority vote ----------------------------------------------------------
payload, votes, agreement = manager_agent.majority_vote(
    [sample("BUY", 0.7), sample("SELL", 0.9), sample("BUY", 0.6)], "T"
)
assert votes == ["BUY", "SELL", "BUY"] and agreement == 0.67, (votes, agreement)
assert payload["decision"] == "BUY" and payload["confidence"] == 0.7, payload

# ---- 1-1 tie without HOLD: summed confidence decides ------------------------
payload, votes, agreement = manager_agent.majority_vote(
    [sample("BUY", 0.5), sample("SELL", 0.9)], "T"
)
assert payload["decision"] == "SELL", payload  # 0.9 beats 0.5 on the tie-break
assert agreement == 0.5, agreement

# ---- 1-1 ties: summed confidence first, HOLD breaks an exact tie ------------
payload, votes, agreement = manager_agent.majority_vote(
    [sample("BUY", 0.9), sample("HOLD", 0.3)], "T"
)
assert payload["decision"] == "BUY", payload  # 0.9 beats 0.3 on the tie-break
assert agreement == 0.5, agreement
payload, votes, agreement = manager_agent.majority_vote(
    [sample("BUY", 0.5), sample("HOLD", 0.5)], "T"
)
assert payload["decision"] == "HOLD", payload  # exact tie: standing aside is cheaper
assert agreement == 0.5, agreement

# ---- malformed samples are not votes; unparseable set falls back ------------
payload, votes, agreement = manager_agent.majority_vote(
    [sample("BUY", 0.8), {"decision": "MAYBE", "confidence": 0.9}], "T"
)
assert payload["decision"] == "BUY" and votes == ["BUY"] and agreement == 1.0, votes
payload, votes, agreement = manager_agent.majority_vote(
    [{"decision": "MAYBE", "confidence": 0.9}], "T"
)
# The raw sample rides through (like the single-sample path) and the
# downstream parse turns the unparseable decision into HOLD.
assert votes == ["HOLD"] and manager_agent.to_manager_result(payload, "T").decision == "HOLD"

# ---- empty and single-sample paths ------------------------------------------
assert manager_agent.majority_vote([], "T") == (None, [], None)
payload, votes, agreement = manager_agent.majority_vote([sample("SELL", 0.7)], "T")
assert payload["decision"] == "SELL" and votes == ["SELL"] and agreement == 1.0

# ---- record fields ride the analysis ----------------------------------------
analysis = StockAnalysis(
    ticker="T", decision="BUY", manager_decision="BUY",
    manager_votes=["BUY", "BUY", "HOLD"], manager_agreement=0.67,
)
assert analysis.manager_votes == ["BUY", "BUY", "HOLD"]
assert analysis.manager_agreement == 0.67
assert StockAnalysis(ticker="T").manager_votes == []
assert StockAnalysis(ticker="T").manager_agreement is None

# ---- the setting is editable in the app -------------------------------------
settings_store._init_db()  # creates user_settings without touching the keychain
snapshot = settings_store.snapshot()
fields = {
    field["name"]: field
    for group in snapshot["groups"]
    for field in group["fields"]
}
assert "manager_votes" in fields, "manager_votes must be exposed on the Settings page"
assert fields["manager_votes"]["env"] == "MANAGER_VOTES"
print("voting math + record fields + settings field OK")

print("VOTING CHECKS PASSED")
