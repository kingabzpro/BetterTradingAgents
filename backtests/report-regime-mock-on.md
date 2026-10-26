# Backtest report - mock mode

Generated 2026-10-26T10:28:29Z

| Setting | Value |
|---|---|
| Mode | mock (rule-based mock) |
| Tickers | NVDA, AMD, MSFT |
| Grid | 2025-06-02 to 2025-07-14 every 21d |
| Horizon | 21 days |
| Depth | pro (excluded: market) |
| Outlook | short_term |
| Fundamentals | excluded |
| Round-trip cost | base 0.10% + participation/volatility term (item 17) |
| Risk regime | enabled (item 18) |
| Short selling | disabled (SELL is cash, graded on relative weakness vs SPY) |
| Portfolio accounting | equal weight per decision date, compounded |
| Benchmarks (buy & hold) | SPY +6.80% · QQQ +7.94% |
| LLM usage | no LLM calls (mock mode) |
| Decision policy | `2026-10-e` · debate rounds 2 |
| Manifest | `manifest-regime-mock-on.json` (revision, data hashes, seeds) |

## Flags

- window: **historical** - Headline performance claims belong to forward paper-trading windows (recent live runs on unresolved outcomes). Historical replays are qualitative evidence only.
- memorization risk: **low (mock mode - no LLM)**
- fundamentals excluded from replay: opt in to the point-in-time SEC EDGAR source with --sec-edgar-fundamentals
- news items filtered to published <= decision date

## Results

Hit rate counts BUY and SELL calls; a long-only SELL is graded on relative weakness vs SPY (right when the stock underperforms), so SELLs are no longer invisible. Cumulative, Sharpe, and max drawdown compound the equal-weight per-date portfolio.

| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 3 | 3 / 0 / 0 | 100.00% | +9.39% | +7.16% | +30.67% | 10.82 | 0.00% | +31.03% |
| AMD | 3 | 3 / 0 / 0 | 100.00% | +15.49% | +13.27% | +53.81% | 17.28 | 0.00% | +54.22% |
| MSFT | 3 | 3 / 0 / 0 | 100.00% | +4.96% | +2.74% | +15.61% | 16.94 | 0.00% | +15.95% |
| overall | 9 | 9 / 0 / 0 | 100.00% | +9.95% | +7.72% | +32.85% | 21.95 | 0.00% | - |

## Manager probability calibration

`probability_beat_spy` scored against its frozen success event (the call's direction beats SPY over the horizon (alpha_pct > 0)); n=9 gradable positioned calls. Brier score: 0.1060 (lower is better; a constant 0.5 prediction scores 0.25).

| Predicted band | n | Mean predicted | Observed rate |
|---|---|---|---|
| 0.70 - 0.79 | 3 | 0.75 | Track record unavailable (n=3 < 30) |
| 0.80 - 0.89 | 6 | 0.80 | Track record unavailable (n=6 < 30) |

## Analyst scorecard

Each researcher's directional calls graded against SPY over the horizon (neutral signals are no-calls and stay out).

| Analyst | Directional calls | Hit rate | Mean alpha |
|---|---|---|---|
| bear | 9 | 11.10% | -7.82% |
| bull | 9 | 88.90% | +7.82% |
| technical | 9 | 88.90% | +7.82% |

## Baselines (the pipeline must beat a cheap baseline to justify its cost)

| Baseline | Decisions | Positioned | Avg alpha | Cumulative | Note |
|---|---|---|---|---|---|
| all HOLD | 9 | - | - | 0.00% | every decision HOLD scores 0 net after costs |
| deterministic momentum | 9 | 4 | +8.25% | +13.53% | 63-day skip-month momentum, volatility-scaled (same costs) |
| buy & hold | per-ticker column above | - | - | per-ticker column | hold each ticker across the graded span |

## Decisions

| Ticker | Date | Decision | Conf. | Prob. | Weight | Entry | Exit | Cost | Net | SPY | Alpha | Regime |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AMD | 2025-06-02 | BUY | 0.72 | 0.72 | 3.8% | 114.63 | 129.58 | 0.10% | +12.94% | +1.55% | +11.39% | risk_on |
| MSFT | 2025-06-02 | BUY | 0.80 | 0.80 | 4.4% | 457.45 | 481.24 | 0.10% | +5.10% | +1.55% | +3.55% | risk_on |
| NVDA | 2025-06-02 | BUY | 0.80 | 0.80 | 4.4% | 137.03 | 143.82 | 0.10% | +4.85% | +1.55% | +3.30% | risk_on |
| AMD | 2025-06-23 | BUY | 0.80 | 0.80 | 4.4% | 129.58 | 146.24 | 0.10% | +12.76% | +4.11% | +8.65% | risk_on |
| MSFT | 2025-06-23 | BUY | 0.76 | 0.76 | 4.1% | 481.24 | 498.10 | 0.10% | +3.40% | +4.11% | -0.71% | risk_on |
| NVDA | 2025-06-23 | BUY | 0.80 | 0.80 | 4.4% | 143.82 | 163.67 | 0.10% | +13.70% | +4.11% | +9.59% | risk_on |
| AMD | 2025-07-14 | BUY | 0.80 | 0.80 | 4.4% | 146.24 | 176.78 | 0.10% | +20.78% | +1.02% | +19.77% | risk_on |
| MSFT | 2025-07-14 | BUY | 0.76 | 0.76 | 4.1% | 498.10 | 530.40 | 0.10% | +6.38% | +1.02% | +5.37% | risk_on |
| NVDA | 2025-07-14 | BUY | 0.80 | 0.80 | 4.4% | 163.67 | 179.56 | 0.10% | +9.61% | +1.02% | +8.59% | risk_on |

---
Educational project - these numbers describe a simulation of a rule-based or LLM pipeline, not investment advice.
