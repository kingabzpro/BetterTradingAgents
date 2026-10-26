# Backtest sweep - mock mode

Generated 2026-10-26T12:28:11Z

| Setting | Value |
|---|---|
| Mode | mock (rule-based, free, deterministic) |
| Tickers | NVDA, AMD, META, MSFT, AAPL, TSLA |
| Grid | 2025-06-02 to 2026-09-14 every 21d |
| Horizon | 21 days, 0.10% round-trip cost |
| Holdout | 2026-02-15 (tune before, untouched test on/after) |
| Seed | 7 |
| Baseline | fast depth + short_term outlook (reference) |

One knob changes per arm; everything else (cache, dates, costs, seed) is
shared with the baseline. Market and fundamental analysts are excluded in
every arm (point-in-time honesty rules in app/backtest/run.py).

| Arm | Decisions | BUY/SELL/HOLD | Tune mean alpha (n) | Test mean alpha (n) | Test cumulative | Test Sharpe | Test max DD |
|---|---|---|---|---|---|---|---|
| [baseline](report-sweep-baseline.md) | 138 | 89 / 25 / 24 | -0.14% (n=64) | +3.45% (n=50) | +43.95% | 3.54 | 0.85% |
| [depth-pro](report-sweep-depth-pro.md) | 138 | 109 / 0 / 29 | +0.03% (n=67) | +2.86% (n=42) | +32.43% | 2.52 | 4.98% |
| [depth-max](report-sweep-depth-max.md) | 138 | 76 / 0 / 62 | +0.29% (n=51) | +2.97% (n=25) | +17.53% | 1.58 | 6.35% |
| [outlook-day](report-sweep-outlook-day.md) | 138 | 88 / 23 / 27 | -0.31% (n=63) | +2.60% (n=48) | +33.49% | 2.42 | 5.06% |
| [outlook-long](report-sweep-outlook-long.md) | 138 | 81 / 21 / 36 | +0.51% (n=54) | +2.91% (n=48) | +37.78% | 2.88 | 4.78% |
| [no-forecast](report-sweep-no-forecast.md) | 138 | 89 / 25 / 24 | -0.14% (n=64) | +3.45% (n=50) | +43.95% | 3.54 | 0.85% |

## Paired differences vs baseline (per-decision net, matched by ticker and date)

### depth-pro - depth=pro: news analyst + bull/bear debate
- tune: mean -0.01%, 95% CI [-0.60%, +0.58%] (crosses 0, n=78)
- test: mean -0.84%, 95% CI [-2.25%, +0.36%] (crosses 0, n=60)

### depth-max - depth=max: adds judge + sentiment (market/fundamental stay excluded)
- tune: mean -0.12%, 95% CI [-0.32%, +0.05%] (crosses 0, n=78)
- test: mean -2.08%, 95% CI [-3.91%, -0.63%] (excludes 0, n=60)

### outlook-day - outlook=day_trade (faster signal windows)
- tune: mean +0.00%, 95% CI [+0.00%, +0.00%] (crosses 0, n=78)
- test: mean -0.74%, 95% CI [-2.04%, +0.13%] (crosses 0, n=60)

### outlook-long - outlook=long_term (slower signal windows)
- tune: mean -0.18%, 95% CI [-0.64%, +0.21%] (crosses 0, n=78)
- test: mean -0.43%, 95% CI [-1.47%, +0.22%] (crosses 0, n=60)

### no-forecast - forecast analyst excluded (technical signals only)
- tune: mean +0.00%, 95% CI [+0.00%, +0.00%] (crosses 0, n=78)
- test: mean +0.00%, 95% CI [+0.00%, +0.00%] (crosses 0, n=60)

## Verdicts (holdout test period)

- baseline: CI [+0.73%, +6.61%] excludes 0 with n=50
- depth-pro: CI [-0.18%, +6.39%] crosses 0: not distinguishable from no edge
- depth-max: insufficient sample (n=25 < 30): do not promote
- outlook-day: CI [-0.38%, +5.79%] crosses 0: not distinguishable from no edge
- outlook-long: CI [-0.15%, +6.50%] crosses 0: not distinguishable from no edge
- no-forecast: CI [+0.73%, +6.61%] excludes 0 with n=50

Reading rule: pick an arm on the tune period, promote it only if the test
period clears n >= 30 positioned decisions and its paired
difference CI excludes 0. Anything else is not distinguishable from the
baseline at this sample size - and mock-mode conclusions describe the
rule-based agents, not the LLM pipeline.

---
Educational project - a simulation sweep, not investment advice.
