"""Offline checks for the deterministic risk-regime gate (phase 4, item 18).

Synthetic SPY/VIX series drive every state: risk_on, trend-only neutral,
non-persistent risk-off, persistent risk-off, fast price shock, fast VIX
jump, slow VIX stress, and the fail-open paths. Then the risk-gate
integration: multiplier scales sizing, zero blocks entries, SELLs never
block. No network and no LLM.

Run: uv run python -m scripts.check_risk_regime
"""

import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "risk_regime_test.db")

from app import risk, risk_regime  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import AgentResult  # noqa: E402


def run(n: int, start: float, step: float) -> list[float]:
    return [round(start + i * step, 4) for i in range(n)]


WOBBLE_VIX = [15.0 + (0.2 if i % 2 else 0.0) for i in range(200)]
RISING = run(200, 100.0, 0.5)

# ---- risk_on ----------------------------------------------------------------
budget = risk_regime.risk_budget(RISING, WOBBLE_VIX)
assert (budget.state, budget.multiplier) == ("risk_on", 1.0), budget
print("risk_on OK")

# ---- stress that only just appeared: score 2 now, 1 a week ago -> neutral --
# Gentle decline keeps the 3-session move small (no fast shock); one high-z
# VIX bar at +4% (below the +25% fast jump) adds the second stress point,
# then disappears from the prior week's score.
gentle_decline = [100.0] * 180 + [
    round(100.0 - 0.3 * (i - 179), 2) for i in range(180, 200)
]
spiking_vix = WOBBLE_VIX[:-1] + [15.6]
budget = risk_regime.risk_budget(gentle_decline, spiking_vix)
assert budget.state == "neutral" and budget.multiplier == 0.75, budget
assert "not yet persistent" in budget.detail, budget.detail
print("non-persistent stress -> neutral OK")

# ---- sustained decline: stress held a week ago too -> risk_off --------------
budget = risk_regime.risk_budget(run(200, 200.0, -0.5), WOBBLE_VIX)
assert (budget.state, budget.multiplier) == ("risk_off", 0.0), budget
assert "26-week mean" in budget.detail, budget.detail
print("persistent risk_off OK")

# ---- fast shocks beat the slow gate -----------------------------------------
price_shock = [100.0] * 197 + [100.0, 95.0, 90.0]
budget = risk_regime.risk_budget(price_shock, WOBBLE_VIX)
assert (budget.state, budget.multiplier) == ("fast_risk_off", 0.0), budget
assert "fast risk-off" in budget.detail, budget.detail

vix_jump = [15.0] * 197 + [15.0, 17.0, 20.0]
budget = risk_regime.risk_budget(RISING, vix_jump)
assert budget.state == "fast_risk_off", budget
print("fast risk-off OK (price shock and VIX jump)")

# ---- slow VIX stress alone: one point -> neutral ----------------------------
elevated_vix = WOBBLE_VIX[:190] + [30.0] * 10
budget = risk_regime.risk_budget(RISING, elevated_vix)
assert budget.state == "neutral" and "VIX" in budget.detail, budget
print("slow VIX stress -> neutral OK")

# ---- fail-open paths --------------------------------------------------------
budget = risk_regime.risk_budget([100.0] * 50, [])
assert (budget.state, budget.multiplier) == ("risk_on", 1.0), budget
assert "too short" in budget.detail, budget.detail
assert risk_regime.robust_zscore([15.0] * 100) == 0.0  # zero MAD never divides
print("fail-open paths OK")

# ---- risk-gate integration --------------------------------------------------
A = lambda: AgentResult(agent="x", signal="bullish", confidence=0.9)  # noqa: E731
base = risk.size_position(0.8, 10.0, settings.default_position_size)

decision, _, size, flags = risk.evaluate(
    "BUY", 0.8, "T", (A(), A(), A()), 10.0, None, regime_multiplier=0.75
)
assert decision == "BUY" and size == round(base * 0.75, 2), (decision, size)
assert any("scaled x0.75" in flag for flag in flags), flags

decision, _, size, flags = risk.evaluate(
    "BUY", 0.8, "T", (A(), A(), A()), 10.0, None, regime_multiplier=0.0
)
assert decision == "HOLD" and size is None, (decision, size)
assert any("risk regime blocks new entries" in flag for flag in flags), flags

decision, _, _, _ = risk.evaluate(
    "SELL", 0.9, "T", (A(), A(), A()), 10.0, None, regime_multiplier=0.0
)
assert decision == "SELL", "exits are never blocked by the regime"

decision, _, size, flags = risk.evaluate("BUY", 0.8, "T", (A(), A(), A()), 10.0, None)
assert decision == "BUY" and size == base and not any("regime" in f for f in flags)
print("risk-gate integration OK (scale, block, exits pass, default unchanged)")

print("RISK REGIME CHECKS PASSED")
