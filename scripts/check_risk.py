"""Offline checks for the risk gate (ROADMAP 1.2). Run: PYTHONPATH=. uv run python scripts/check_risk.py"""

import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "risk_test.db")

from app import risk  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import AgentResult, PortfolioPosition, PortfolioSummary  # noqa: E402

D = settings.default_position_size

# ---- sizing: dollar-vol parity scaled by conviction -------------------------
assert risk.size_position(0.8, 10.0, D) == round(D * 0.8, 2)   # vol below target: scale 1.0
assert risk.size_position(0.8, 30.0, D) == round(D * 0.4, 2)   # 30% vol halves the size
assert risk.size_position(0.9, 60.0, D) == round(D * 0.25, 2)  # clamped at the floor
assert risk.size_position(1.0, None, D) == D                   # no vol info: unscaled
assert risk.size_position(0.0, 10.0, D) == round(D * 0.25, 2)  # floor guards zero conviction
print("size_position OK")


def flat_portfolio(equity=100_000.0, cash=100_000.0):
    return PortfolioSummary(
        cash=cash,
        positions_value=equity - cash, total_equity=equity,
    )


def position(ticker, value):
    return PortfolioPosition(
        id=1, ticker=ticker, quantity=1, entry_price=value,
        current_price=value, cost=value, value=value,
    )


A = lambda: AgentResult(agent="x", signal="bullish", confidence=0.9)  # noqa: E731

# ---- healthy BUY: sized, no flags -------------------------------------------
d, c, s, f = risk.evaluate("BUY", 0.8, "NVDA", (A(), A(), A()), 30.0, flat_portfolio())
assert (d, c, f) == ("BUY", 0.8, []) and s == round(D * 0.4, 2)
print("healthy BUY OK:", s)

# ---- per-ticker cap: existing 9.5% holding + size breaches 10% --------------
pf = flat_portfolio(cash=90_500.0)
pf.positions = [position("NVDA", 9_500.0)]
d, c, s, f = risk.evaluate("BUY", 0.8, "NVDA", (A(), A(), A()), 10.0, pf)
assert d == "HOLD" and s is None
assert any("exceed 10% of equity" in flag for flag in f), f
print("per-ticker cap OK:", f)

# ---- invested cap: already 59% invested --------------------------------------
pf = flat_portfolio(equity=100_000.0, cash=41_000.0)
pf.positions_value, pf.total_equity = 59_000.0, 100_000.0
d, c, s, f = risk.evaluate("BUY", 0.8, "AMD", (A(), A(), A()), 10.0, pf)
assert d == "HOLD" and any("invested capital" in flag for flag in f), f
print("invested cap OK:", f)

# ---- cash floor: spending the size would leave <10% cash ---------------------
pf = flat_portfolio(cash=11_000.0)
pf.positions_value, pf.total_equity = 89_000.0, 100_000.0
d, c, s, f = risk.evaluate("BUY", 0.8, "AMD", (A(), A(), A()), 10.0, pf)
assert d == "HOLD" and any("cash buffer" in flag for flag in f), f
print("cash floor OK:", f)

# ---- drawdown brake: down past MAX_DRAWDOWN_PCT halts BUYs, not SELLs -------
pf = flat_portfolio(equity=110_000.0, cash=70_000.0)
pf.positions = [position("MSFT", 40_000.0)]
pf.total_pnl = -20_000.0  # start 130k -> down 15.4% > 15% brake
d, c, s, f_brake = risk.evaluate("BUY", 0.8, "AMD", (A(), A(), A()), 10.0, pf)
assert d == "HOLD" and any("drawdown brake" in fl for fl in f_brake), f_brake
d, c, s, f = risk.evaluate("SELL", 0.9, "MSFT", (A(), A(), A()), 10.0, pf)
assert d == "SELL", f  # reducing exposure in a drawdown still passes
print("drawdown brake OK:", f_brake)

pf.total_pnl = -19_000.0  # down 14.7%: inside the brake, BUY proceeds
d, c, s, f = risk.evaluate("BUY", 0.8, "AMD", (A(), A(), A()), 10.0, pf)
assert d == "BUY" and f == [], f
pf.total_pnl = None  # P&L unknown: the brake stays silent, other caps still run
d, c, s, f = risk.evaluate("BUY", 0.8, "AMD", (A(), A(), A()), 10.0, pf)
assert d == "BUY" and f == [], f
print("drawdown brake thresholds OK")

# ---- position count cap: 11th name halts, adding to a held name passes ------
pf = flat_portfolio(equity=100_000.0, cash=95_000.0)
pf.positions = [position(f"T{i}", 500.0) for i in range(settings.max_positions)]
d, c, s, f_cap = risk.evaluate("BUY", 0.8, "AMD", (A(), A(), A()), 10.0, pf)
assert d == "HOLD" and any("position cap" in fl for fl in f_cap), f_cap
d, c, s, f = risk.evaluate("BUY", 0.8, "T0", (A(), A(), A()), 10.0, pf)
assert d == "BUY", f  # existing holder tops up: count cap exempt
print("position count cap OK:", f_cap)

# ---- missing-input brake: 2/3 analysts failed --------------------------------
d, c, s, f = risk.evaluate("BUY", 0.9, "NVDA", (A(), None, None), 30.0, flat_portfolio())
assert d == "BUY" and c == 0.5
assert any("capped at 50%" in flag for flag in f), f
assert s == round(D * 0.5 * 0.5, 2)  # sized with the capped confidence
print("missing-input brake OK:", c, s)

# ---- portfolio unavailable: BUY proceeds, flagged -----------------------------
d, c, s, f = risk.evaluate("BUY", 0.8, "NVDA", (A(), A(), A()), 30.0, None)
assert d == "BUY" and any("caps skipped" in flag for flag in f), f
print("no-portfolio OK:", f)

# ---- SELL/HOLD pass through untouched ----------------------------------------
d, c, s, f = risk.evaluate("SELL", 0.9, "NVDA", (A(), A(), A()), 30.0, flat_portfolio())
assert (d, s, f) == ("SELL", None, [])
d, c, s, f = risk.evaluate("HOLD", 0.4, "NVDA", (None, None, None), 30.0, flat_portfolio())
assert d == "HOLD" and s is None and any("capped" in fl for fl in f)
print("passthrough OK")

# ---- forecast-aware gate: z = forecast / 5-day noise band --------------------
assert abs(risk.forecast_noise_pct(44.92) - 6.33) < 0.01
assert abs(risk.forecast_z(-2.11, 44.92) - (-0.33)) < 0.01
assert risk.forecast_signal(None) == "unavailable"
assert risk.forecast_signal(-1.2) == "clearly_bearish"
assert risk.forecast_signal(-0.33) == "no_edge statistical noise"

# NVDA-style case: -2.11% against a +/-6.3% band is noise -> BUY untouched
d, c, s, f = risk.evaluate("BUY", 0.8, "NVDA", (A(), A(), A()), 44.9, flat_portfolio(), forecast_change_pct=-2.11)
assert d == "BUY" and s == risk.size_position(0.8, 44.9, D) and f == [], f
print("forecast noise passthrough OK: z=-0.33")

# past half the band against the BUY -> size halved, flagged
d, c, s, f = risk.evaluate("BUY", 0.8, "NVDA", (A(), A(), A()), 44.9, flat_portfolio(), forecast_change_pct=-4.0)
assert d == "BUY" and s == round(risk.size_position(0.8, 44.9, D) * 0.5, 2)
assert any("halved" in fl for fl in f), f
print("forecast half-size OK:", s, f)

# beyond the band against the BUY -> downgraded to HOLD
d, c, s, f = risk.evaluate("BUY", 0.8, "NVDA", (A(), A(), A()), 44.9, flat_portfolio(), forecast_change_pct=-8.0)
assert d == "HOLD" and s is None and any("noise band" in fl for fl in f), f
print("forecast veto OK:", f)

# symmetric: a clearly bullish forecast vetoes a SELL
d, c, s, f = risk.evaluate("SELL", 0.9, "NVDA", (A(), A(), A()), 44.9, flat_portfolio(), forecast_change_pct=8.0)
assert d == "HOLD" and any("noise band" in fl for fl in f), f
print("forecast SELL veto OK")

# ---- concentration check (ROADMAP P1.3): correlated holdings = one bet ------
from app.models import ConcentrationCheck  # noqa: E402

# correlation helper: identical series -> ~1, mirrored -> ~-1, thin/flat -> None
base = [100 + (i % 7) * 2 + (i * 13 % 5) for i in range(60)]
assert risk.returns_correlation(base, list(base)) > 0.99
assert risk.returns_correlation(base, [200 - x for x in base]) < -0.99
assert risk.returns_correlation(base[:15], base[:15]) is None  # under MIN_RETURN_OBS
assert risk.returns_correlation([100.0] * 60, base) is None  # flat series
print("returns_correlation OK")

# perfect correlation pulls MSFT into the group: 20k + 10k BUY = 30% > 25% cap
pf = flat_portfolio(cash=80_000.0)
pf.positions = [position("MSFT", 20_000.0)]
conc = risk.concentration_check(
    "AMD", 10_000.0, pf,
    held_closes={"MSFT": base}, candidate_closes=base,
)
assert conc.status == "high" and conc.correlated == ["MSFT"], conc
assert conc.before_pct == 20.0 and conc.after_pct == 30.0 and conc.cap_pct == 25.0
assert "20.0%" in conc.detail and "30.0%" in conc.detail
print("concentration high OK:", conc.detail)

# same numbers but mirrored returns: MSFT is a hedge, group stays within cap
conc = risk.concentration_check(
    "AMD", 10_000.0, pf,
    held_closes={"MSFT": [200 - x for x in base]}, candidate_closes=base,
)
assert conc.status == "ok" and conc.after_pct == 10.0, conc
print("concentration ok OK")

# missing history is never read as independent: reported unknown
conc = risk.concentration_check("AMD", 10_000.0, pf, held_closes={}, candidate_closes=base)
assert conc.status == "unknown" and conc.unverified == ["MSFT"], conc
conc = risk.concentration_check("AMD", 10_000.0, pf, held_closes={"MSFT": base}, candidate_closes=base[:10])
assert conc.status == "unknown", conc
print("concentration unknown OK:", conc.detail)

# existing stake in the same ticker joins the group; no positions -> ok
pf = flat_portfolio(cash=80_000.0)
pf.positions = [position("AMD", 20_000.0)]
conc = risk.concentration_check("AMD", 10_000.0, pf, held_closes={}, candidate_closes=None)
assert conc.status == "high" and conc.correlated == [], conc  # self-group needs no history
conc = risk.concentration_check("AMD", 10_000.0, flat_portfolio(), held_closes={}, candidate_closes=base)
assert conc.status == "ok", conc
assert risk.concentration_check("AMD", 10_000.0, flat_portfolio(equity=0.0)).status == "unknown"
assert ConcentrationCheck().status == "unknown"  # default is honest
print("concentration edges OK")

# ---- mock e2e: StockAnalysis carries size + flags -----------------------------
from app.config import settings as _settings  # noqa: E402

_settings.llm_api_key = ""  # force mock mode
# Offline checks never read a live paper account: the manager then weighs the
# research without holdings, which is the honest unconfigured path asserted below.
_settings.alpaca_api_key_id = _settings.alpaca_api_secret_key = ""

from app.workflow import analyze_ticker  # noqa: E402
import asyncio  # noqa: E402

async def e2e():
    # No paper account configured in checks: the manager weighs research
    # without holdings, which is the honest unconfigured path.
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    result = await asyncio.wait_for(analyze_ticker("NVDA", emit), timeout=90)
    assert result.error is None, result.error
    assert isinstance(result.risk_flags, list)
    if result.forecast_method:  # forecast researcher ran -> band + z must flow
        assert result.forecast_band_pct and result.forecast_band_pct > 0, result.forecast_band_pct
        assert result.forecast_z is not None, result.forecast_z
        print("e2e forecast assessment OK: band +/-%.1f%% z=%.2f"
              % (result.forecast_band_pct, result.forecast_z))
    if result.decision == "BUY":
        assert result.suggested_size_usd and result.suggested_size_usd <= D * 1.5
        # No paper account in checks: no portfolio, so the correlated-exposure
        # gate is honestly "not checked" (None) rather than a fake clean pass.
        assert result.concentration is None
        print("e2e BUY OK: size", result.suggested_size_usd,
              "flags", result.risk_flags)
    else:
        assert result.suggested_size_usd is None
        print("e2e OK:", result.decision, "flags", result.risk_flags)

asyncio.run(e2e())
print("ALL RISK CHECKS PASSED")
