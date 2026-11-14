# Backtest report - llm mode

Generated 2026-10-28T07:11:06Z

| Setting | Value |
|---|---|
| Mode | llm (deepseek-ai/DeepSeek-V4.1-Flash) |
| Tickers | NVDA, AMD, MSFT |
| Grid | 2026-09-03 to 2026-09-25 every 22d |
| Horizon | 21 days |
| Depth | pro (excluded: market) |
| Outlook | short_term |
| Fundamentals | excluded |
| Round-trip cost | 0.10% |
| Short selling | disabled (SELL scores 0) |
| Decision policy | `2026-10-e` · debate rounds 2 |
| Manifest | `manifest-vs-upstream-pro.json` (revision, data hashes, seeds) |

## Flags

- memorization risk: **high** - LLM-mode backtests on dates inside the model's training data are inflated: the model may already know how each window resolved. Prefer mock mode or very recent dates for headline numbers.
- fundamentals excluded from replay: opt in to the point-in-time SEC EDGAR source with --sec-edgar-fundamentals
- news items filtered to published <= decision date

## Results

| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 2 | 2 / 0 / 0 | 0.00% | -3.32% | -2.54% | -6.56% | -8.25 | 6.56% | -6.16% |
| AMD | 2 | 0 / 1 / 1 | - | +0.00% | - | +0.00% | 0.0 | 0.00% | +15.91% |
| MSFT | 2 | 2 / 0 / 0 | 50.00% | -0.05% | +0.75% | -0.15% | -0.07 | 2.49% | +3.71% |
| overall | 6 | 4 / 1 / 1 | 25.00% | -1.12% | -0.90% | -6.70% | -1.98 | 6.70% | - |

## Baselines (the pipeline must beat a cheap baseline to justify its cost)

| Baseline | Decisions | Positioned | Avg alpha | Cumulative | Note |
|---|---|---|---|---|---|
| all HOLD | 6 | - | - | 0.00% | every decision HOLD scores 0 net after costs |
| deterministic momentum | 6 | 4 | +9.68% | +35.33% | 63-day skip-month momentum, volatility-scaled (same costs) |
| buy & hold | per-ticker column above | - | - | per-ticker column | hold each ticker across the graded span |

## Decisions

| Ticker | Date | Decision | Conf. | Entry | Exit | Net | SPY | Alpha |
|---|---|---|---|---|---|---|---|---|
| AMD | 2026-09-03 | SELL | 0.70 | 456.16 | 629.26 | +0.00% | -0.53% | - |
| MSFT | 2026-09-03 | BUY | 0.60 | 510.12 | 497.93 | -2.49% | -0.53% | -1.96% |
| NVDA | 2026-09-03 | BUY | 0.60 | 228.19 | 224.58 | -1.68% | -0.53% | -1.16% |
| AMD | 2026-09-25 | HOLD | 0.55 | 630.63 | 528.75 | +0.00% | -1.05% | - |
| MSFT | 2026-09-25 | BUY | 0.60 | 516.17 | 529.05 | +2.40% | -1.05% | +3.45% |
| NVDA | 2026-09-25 | BUY | 0.60 | 225.07 | 214.13 | -4.96% | -1.05% | -3.91% |

---
Educational project - these numbers describe a simulation of a rule-based or LLM pipeline, not investment advice.
