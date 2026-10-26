# Backtest report - mock mode

Generated 2026-10-26T10:28:57Z

| Setting | Value |
|---|---|
| Mode | mock (rule-based mock) |
| Tickers | NVDA, AMD, MSFT |
| Grid | 2025-11-17 to 2025-12-08 every 21d |
| Horizon | 21 days |
| Depth | pro (excluded: market) |
| Outlook | short_term |
| Fundamentals | excluded |
| Round-trip cost | base 0.10% + participation/volatility term (item 17) |
| Risk regime | off |
| Short selling | disabled (SELL is cash, graded on relative weakness vs SPY) |
| Portfolio accounting | equal weight per decision date, compounded |
| Benchmarks (buy & hold) | SPY +3.64% · QQQ +2.98% |
| LLM usage | no LLM calls (mock mode) |
| Decision policy | `2026-10-e` · debate rounds 2 |
| Manifest | `manifest-regime-stress-off.json` (revision, data hashes, seeds) |

## Flags

- window: **historical** - Headline performance claims belong to forward paper-trading windows (recent live runs on unresolved outcomes). Historical replays are qualitative evidence only.
- memorization risk: **low (mock mode - no LLM)**
- fundamentals excluded from replay: opt in to the point-in-time SEC EDGAR source with --sec-edgar-fundamentals
- news items filtered to published <= decision date

## Results

Hit rate counts BUY and SELL calls; a long-only SELL is graded on relative weakness vs SPY (right when the stock underperforms), so SELLs are no longer invisible. Cumulative, Sharpe, and max drawdown compound the equal-weight per-date portfolio.

| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 2 | 1 / 0 / 1 | 100.00% | +0.67% | +0.42% | +1.34% | 4.17 | 0.00% | +0.87% |
| AMD | 2 | 2 / 0 / 0 | 0.00% | -5.38% | -7.18% | -10.55% | -8.04 | 10.55% | -10.36% |
| MSFT | 2 | 2 / 0 / 0 | 0.00% | -2.03% | -3.83% | -4.03% | -7.49 | 4.03% | -3.84% |
| overall | 6 | 5 / 0 / 1 | 20.00% | -2.25% | -4.32% | -4.47% | -6.12 | 4.47% | - |

## Manager probability calibration

`probability_beat_spy` scored against its frozen success event (the call's direction beats SPY over the horizon (alpha_pct > 0)); n=5 gradable positioned calls. Brier score: 0.3665 (lower is better; a constant 0.5 prediction scores 0.25).

| Predicted band | n | Mean predicted | Observed rate |
|---|---|---|---|
| 0.60 - 0.69 | 5 | 0.66 | Track record unavailable (n=5 < 30) |

## Analyst scorecard

Each researcher's directional calls graded against SPY over the horizon (neutral signals are no-calls and stay out).

| Analyst | Directional calls | Hit rate | Mean alpha |
|---|---|---|---|
| bear | 6 | 83.30% | +4.06% |
| bull | 6 | 16.70% | -4.06% |
| technical | 2 | 50.00% | -0.59% |

## Baselines (the pipeline must beat a cheap baseline to justify its cost)

| Baseline | Decisions | Positioned | Avg alpha | Cumulative | Note |
|---|---|---|---|---|---|
| all HOLD | 6 | - | - | 0.00% | every decision HOLD scores 0 net after costs |
| deterministic momentum | 6 | 5 | -4.63% | -4.40% | 63-day skip-month momentum, volatility-scaled (same costs) |
| buy & hold | per-ticker column above | - | - | per-ticker column | hold each ticker across the graded span |

## Decisions

| Ticker | Date | Decision | Conf. | Prob. | Weight | Entry | Exit | Cost | Net | SPY | Alpha | Regime |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AMD | 2025-11-17 | BUY | 0.65 | 0.65 | 3.2% | 240.52 | 221.11 | 0.10% | -8.17% | +2.70% | -10.87% | - |
| MSFT | 2025-11-17 | BUY | 0.65 | 0.65 | 3.2% | 503.35 | 487.92 | 0.10% | -3.16% | +2.70% | -5.86% | - |
| NVDA | 2025-11-17 | HOLD | 0.53 | - | - | 186.15 | 185.12 | 0.10% | +0.00% | +2.70% | - | - |
| AMD | 2025-12-08 | BUY | 0.65 | 0.65 | 3.2% | 221.11 | 215.61 | 0.10% | -2.59% | +0.91% | -3.50% | - |
| MSFT | 2025-12-08 | BUY | 0.68 | 0.68 | 3.4% | 487.92 | 484.03 | 0.10% | -0.90% | +0.91% | -1.81% | - |
| NVDA | 2025-12-08 | BUY | 0.68 | 0.68 | 3.4% | 185.12 | 187.78 | 0.10% | +1.34% | +0.91% | +0.42% | - |

---
Educational project - these numbers describe a simulation of a rule-based or LLM pipeline, not investment advice.
