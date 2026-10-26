# Paired experiment - depth

Generated from reports `C:\Repository\GitHub\BetterTradingAgents\backtests\report-mock-depth-left.md` and `C:\Repository\GitHub\BetterTradingAgents\backtests\report-mock-depth-right.md`; same cache, same dates, same costs, same seed. Only `depth` differs.

Scope: **test period (dates >= 2026-02-15)**. Sample sizes and intervals below; do not quote either arm without them.

| Arm | Decisions | Positioned (alpha) | Mean alpha | Cumulative |
|---|---|---|---|---|
| A (depth=fast) | 60 | 50 | +3.45% | +43.95% |
| B (depth=max) | 60 | 25 | +2.97% | +17.53% |

## Paired difference (A - B, per-decision net, matched by ticker and date)

- paired graded decisions: 60 (A alone graded 60, B alone 60)
- mean difference: +2.08%
- 95% bootstrap CI over 2000 resamples: [+0.63%, +3.91%]
- verdict: difference CI [+0.63%, +3.91%] excludes 0 with n=60: depth=fast is the better arm on this scope only - one scope, one change, no stacking of tuned winners.

- arm A verdict on its own: CI [+0.73%, +6.61%] excludes 0 with n=50.
- arm B verdict on its own: insufficient sample (n=25 < 30): do not promote.

---
Educational project - a simulation comparison, not investment advice.
