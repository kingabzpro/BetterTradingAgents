# Backtest report - mock mode

Generated 2026-09-04T16:10:26Z

| Setting | Value |
|---|---|
| Mode | mock (rule-based mock) |
| Tickers | NVDA, AMD, META |
| Grid | 2026-03-11 to 2026-06-24 every 21d |
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
| NVDA | 6 | 2 / 0 / 4 | 0.00% | -2.16% | -3.78% | -12.55% | -2.91 | 12.55% | +14.36% |
| AMD | 6 | 4 / 0 / 2 | 75.00% | +3.60% | +5.61% | +21.44% | 1.8 | 4.30% | +158.33% |
| META | 6 | 2 / 0 / 4 | 50.00% | -1.59% | -4.33% | -9.67% | -1.66 | 10.50% | +4.22% |
| overall | 18 | 8 / 0 / 10 | 50.00% | -0.05% | +0.78% | -4.07% | -0.03 | 20.63% | - |

## Decisions

| Ticker | Date | Decision | Conf. | Entry | Exit | Net | SPY | Alpha |
|---|---|---|---|---|---|---|---|---|
| AMD | 2026-03-11 | BUY | 0.70 | 204.83 | 210.21 | +2.53% | -2.85% | +5.38% |
| META | 2026-03-11 | HOLD | 0.61 | 653.69 | 578.69 | +0.00% | -2.85% | - |
| NVDA | 2026-03-11 | BUY | 0.70 | 185.81 | 175.55 | -5.63% | -2.85% | -2.77% |
| AMD | 2026-04-01 | HOLD | 0.77 | 210.21 | 303.46 | +0.00% | +8.54% | - |
| META | 2026-04-01 | HOLD | 0.51 | 578.69 | 674.10 | +0.00% | +8.54% | - |
| NVDA | 2026-04-01 | HOLD | 0.61 | 175.55 | 202.26 | +0.00% | +8.54% | - |
| AMD | 2026-04-22 | HOLD | 0.85 | 303.46 | 445.50 | +0.00% | +4.37% | - |
| META | 2026-04-22 | HOLD | 0.77 | 674.10 | 616.06 | +0.00% | +4.37% | - |
| NVDA | 2026-04-22 | HOLD | 0.85 | 202.26 | 225.57 | +0.00% | +4.37% | - |
| AMD | 2026-05-13 | BUY | 0.81 | 445.50 | 542.52 | +21.68% | +1.61% | +20.07% |
| META | 2026-05-13 | BUY | 0.70 | 616.06 | 622.40 | +0.93% | +1.61% | -0.68% |
| NVDA | 2026-05-13 | HOLD | 0.85 | 225.57 | 214.50 | +0.00% | +1.61% | - |
| AMD | 2026-06-03 | BUY | 0.89 | 542.52 | 519.74 | -4.30% | -2.53% | -1.77% |
| META | 2026-06-03 | BUY | 0.69 | 622.40 | 557.67 | -10.50% | -2.53% | -7.97% |
| NVDA | 2026-06-03 | BUY | 0.73 | 214.50 | 199.00 | -7.33% | -2.53% | -4.79% |
| AMD | 2026-06-24 | BUY | 0.89 | 519.74 | 529.14 | +1.71% | +2.94% | -1.23% |
| META | 2026-06-24 | HOLD | 0.61 | 557.67 | 681.31 | +0.00% | +2.94% | - |
| NVDA | 2026-06-24 | HOLD | 0.61 | 199.00 | 212.50 | +0.00% | +2.94% | - |

---
Educational project - these numbers describe a simulation of a rule-based or LLM pipeline, not investment advice.
