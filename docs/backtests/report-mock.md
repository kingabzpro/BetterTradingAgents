# Backtest report - mock mode

Generated 2026-09-09T17:48:07Z

| Setting | Value |
|---|---|
| Mode | mock (rule-based mock) |
| Tickers | NVDA, AMD, META |
| Grid | 2026-03-16 to 2026-06-29 every 21d |
| Horizon | 21 days |
| Depth | fast |
| Outlook | short_term |
| Round-trip cost | 0.10% |
| Short selling | disabled (SELL scores 0) |

## Flags

- memorization risk: **low (mock mode - no LLM)**
- fundamentals are current-vintage, not point-in-time - a known bias, stated here per ROADMAP 2.2
- news items filtered to published <= decision date

## Results

| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 6 | 4 / 0 / 2 | 25.00% | +0.98% | -0.49% | +3.39% | 0.42 | 12.38% | +11.08% |
| AMD | 6 | 3 / 0 / 3 | 66.70% | +3.26% | +6.27% | +19.28% | 1.78 | 6.76% | +156.17% |
| META | 6 | 3 / 0 / 3 | 0.00% | -2.83% | -5.44% | -16.12% | -3.65 | 16.12% | +3.03% |
| overall | 18 | 10 / 0 / 8 | 30.00% | +0.47% | +0.05% | +3.45% | 0.25 | 17.42% | - |

## Decisions

| Ticker | Date | Decision | Conf. | Entry | Exit | Net | SPY | Alpha |
|---|---|---|---|---|---|---|---|---|
| AMD | 2026-03-16 | HOLD | 0.50 | 196.58 | 220.18 | +0.00% | -1.24% | - |
| META | 2026-03-16 | BUY | 0.50 | 626.87 | 572.49 | -8.77% | -1.24% | -7.53% |
| NVDA | 2026-03-16 | BUY | 0.50 | 183.01 | 177.43 | -3.15% | -1.24% | -1.90% |
| AMD | 2026-04-06 | HOLD | 0.50 | 220.18 | 334.63 | +0.00% | +8.54% | - |
| META | 2026-04-06 | HOLD | 0.50 | 572.49 | 677.99 | +0.00% | +8.54% | - |
| NVDA | 2026-04-06 | BUY | 0.50 | 177.43 | 216.36 | +21.84% | +8.54% | +13.30% |
| AMD | 2026-04-27 | HOLD | 0.50 | 334.63 | 420.99 | +0.00% | +3.28% | - |
| META | 2026-04-27 | HOLD | 0.50 | 677.99 | 610.64 | +0.00% | +3.28% | - |
| NVDA | 2026-04-27 | HOLD | 0.50 | 216.36 | 222.06 | +0.00% | +3.28% | - |
| AMD | 2026-05-18 | BUY | 0.50 | 420.99 | 490.33 | +16.37% | +0.08% | +16.29% |
| META | 2026-05-18 | BUY | 0.50 | 610.64 | 584.85 | -4.32% | +0.08% | -4.40% |
| NVDA | 2026-05-18 | BUY | 0.50 | 222.06 | 208.64 | -6.14% | +0.08% | -6.22% |
| AMD | 2026-06-08 | BUY | 0.50 | 490.33 | 539.49 | +9.93% | +0.50% | +9.43% |
| META | 2026-06-08 | BUY | 0.50 | 584.85 | 562.60 | -3.90% | +0.50% | -4.40% |
| NVDA | 2026-06-08 | BUY | 0.50 | 208.64 | 194.97 | -6.65% | +0.50% | -7.15% |
| AMD | 2026-06-29 | BUY | 0.50 | 539.49 | 503.57 | -6.76% | +0.15% | -6.91% |
| META | 2026-06-29 | HOLD | 0.50 | 562.60 | 645.85 | +0.00% | +0.15% | - |
| NVDA | 2026-06-29 | HOLD | 0.50 | 194.97 | 203.28 | +0.00% | +0.15% | - |

---
Educational project - these numbers describe a simulation of a rule-based or LLM pipeline, not investment advice.
