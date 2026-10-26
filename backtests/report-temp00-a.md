# Backtest report - llm mode

Generated 2026-10-26T10:39:16Z

| Setting | Value |
|---|---|
| Mode | llm (deepseek-ai/DeepSeek-V4.1-Flash) |
| Tickers | NVDA, AMD, MSFT, AAPL, TSLA |
| Grid | 2025-06-02 to 2025-08-04 every 21d |
| Horizon | 21 days |
| Depth | pro (excluded: market) |
| Outlook | short_term |
| Fundamentals | excluded |
| Round-trip cost | base 0.10% + participation/volatility term (item 17) |
| Risk regime | off |
| Short selling | disabled (SELL is cash, graded on relative weakness vs SPY) |
| Portfolio accounting | equal weight per decision date, compounded |
| Benchmarks (buy & hold) | SPY +8.72% · QQQ +9.13% |
| LLM usage | 124,225 prompt + 70,449 completion = 194,674 tokens · ~$0.1218 across 20 priced run(s) |
| Decision policy | `2026-10-e` · debate rounds 2 |
| Manifest | `manifest-temp00-a.json` (revision, data hashes, seeds) |

## Flags

- window: **historical** - Headline performance claims belong to forward paper-trading windows (recent live runs on unresolved outcomes). Historical replays are qualitative evidence only.
- memorization risk: **high** - LLM-mode backtests on dates inside the model's training data are inflated: the model may already know how each window resolved. Prefer mock mode or very recent dates for headline numbers.
- fundamentals excluded from replay: opt in to the point-in-time SEC EDGAR source with --sec-edgar-fundamentals
- news items filtered to published <= decision date

## Results

Hit rate counts BUY and SELL calls; a long-only SELL is graded on relative weakness vs SPY (right when the stock underperforms), so SELLs are no longer invisible. Cumulative, Sharpe, and max drawdown compound the equal-weight per-date portfolio.

| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 4 | 3 / 0 / 1 | 66.70% | +4.58% | +3.63% | +18.96% | 3.39 | 0.21% | +30.89% |
| AMD | 4 | 2 / 0 / 2 | 100.00% | +6.42% | +10.02% | +27.35% | 4.17 | 0.00% | +42.51% |
| MSFT | 4 | 3 / 0 / 1 | 66.70% | +1.42% | +0.44% | +5.32% | 1.23 | 5.80% | +9.33% |
| AAPL | 4 | 1 / 2 / 1 | 33.30% | -0.66% | -4.01% | -2.63% | -2.41 | 2.63% | +12.75% |
| TSLA | 4 | 1 / 1 / 2 | 0.00% | -2.30% | -11.80% | -9.21% | -2.41 | 9.21% | +1.14% |
| overall | 20 | 10 / 3 / 7 | 53.80% | +1.89% | -0.26% | +7.69% | 3.49 | 1.20% | - |

## Manager probability calibration

`probability_beat_spy` scored against its frozen success event (the call's direction beats SPY over the horizon (alpha_pct > 0)); n=13 gradable positioned calls. Brier score: 0.2520 (lower is better; a constant 0.5 prediction scores 0.25).

| Predicted band | n | Mean predicted | Observed rate |
|---|---|---|---|
| 0.50 - 0.59 | 13 | 0.55 | Track record unavailable (n=13 < 30) |

## Analyst scorecard

Each researcher's directional calls graded against SPY over the horizon (neutral signals are no-calls and stay out).

| Analyst | Directional calls | Hit rate | Mean alpha |
|---|---|---|---|
| bear | 20 | 45.00% | -2.49% |
| bull | 20 | 55.00% | +2.49% |
| technical | 19 | 57.90% | +1.04% |

## Baselines (the pipeline must beat a cheap baseline to justify its cost)

| Baseline | Decisions | Positioned | Avg alpha | Cumulative | Note |
|---|---|---|---|---|---|
| all HOLD | 20 | - | - | 0.00% | every decision HOLD scores 0 net after costs |
| deterministic momentum | 20 | 9 | +2.29% | +7.21% | 63-day skip-month momentum, volatility-scaled (same costs) |
| buy & hold | per-ticker column above | - | - | per-ticker column | hold each ticker across the graded span |

## Decisions

| Ticker | Date | Decision | Conf. | Prob. | Weight | Entry | Exit | Cost | Net | SPY | Alpha | Regime |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AAPL | 2025-06-02 | SELL | 0.60 | 0.55 | 0.0% | 200.73 | 200.53 | 0.10% | +0.00% | +1.55% | +1.65% | - |
| AMD | 2025-06-02 | BUY | 0.60 | 0.55 | 5.0% | 114.63 | 129.58 | 0.10% | +12.94% | +1.55% | +11.39% | - |
| MSFT | 2025-06-02 | BUY | 0.58 | 0.55 | 4.0% | 457.45 | 481.24 | 0.10% | +5.10% | +1.55% | +3.55% | - |
| NVDA | 2025-06-02 | BUY | 0.60 | 0.56 | 4.0% | 137.03 | 143.82 | 0.10% | +4.85% | +1.55% | +3.30% | - |
| TSLA | 2025-06-02 | HOLD | 0.55 | - | - | 342.69 | 348.68 | 0.10% | +0.00% | +1.55% | - | - |
| AAPL | 2025-06-23 | HOLD | 0.50 | - | - | 200.53 | 207.62 | 0.10% | +0.00% | +4.11% | - | - |
| AMD | 2025-06-23 | BUY | 0.60 | 0.53 | 3.0% | 129.58 | 146.24 | 0.10% | +12.76% | +4.11% | +8.65% | - |
| MSFT | 2025-06-23 | HOLD | 0.55 | - | - | 481.24 | 498.10 | 0.10% | +0.00% | +4.11% | - | - |
| NVDA | 2025-06-23 | BUY | 0.60 | 0.55 | 5.0% | 143.82 | 163.67 | 0.10% | +13.70% | +4.11% | +9.59% | - |
| TSLA | 2025-06-23 | BUY | 0.62 | 0.56 | 6.0% | 348.68 | 316.90 | 0.10% | -9.21% | +4.11% | -13.32% | - |
| AAPL | 2025-07-14 | BUY | 0.60 | 0.55 | 6.0% | 207.62 | 202.37 | 0.10% | -2.63% | +1.02% | -3.64% | - |
| AMD | 2025-07-14 | HOLD | 0.55 | - | - | 146.24 | 176.78 | 0.10% | +0.00% | +1.02% | - | - |
| MSFT | 2025-07-14 | BUY | 0.60 | 0.55 | 5.0% | 498.10 | 530.40 | 0.10% | +6.38% | +1.02% | +5.37% | - |
| NVDA | 2025-07-14 | HOLD | 0.55 | - | - | 163.67 | 179.56 | 0.10% | +0.00% | +1.02% | - | - |
| TSLA | 2025-07-14 | HOLD | 0.50 | - | - | 316.90 | 309.26 | 0.10% | +0.00% | +1.02% | - | - |
| AAPL | 2025-08-04 | SELL | 0.62 | 0.58 | 0.0% | 202.37 | 226.32 | 0.10% | +0.00% | +1.79% | -10.05% | - |
| AMD | 2025-08-04 | HOLD | 0.60 | - | - | 176.78 | 163.36 | 0.10% | +0.00% | +1.79% | - | - |
| MSFT | 2025-08-04 | BUY | 0.55 | 0.53 | 4.0% | 530.40 | 500.14 | 0.10% | -5.80% | +1.79% | -7.59% | - |
| NVDA | 2025-08-04 | BUY | 0.62 | 0.56 | 6.0% | 179.56 | 179.37 | 0.10% | -0.21% | +1.79% | -2.00% | - |
| TSLA | 2025-08-04 | SELL | 0.60 | 0.55 | 0.0% | 309.26 | 346.60 | 0.10% | +0.00% | +1.79% | -10.28% | - |

---
Educational project - these numbers describe a simulation of a rule-based or LLM pipeline, not investment advice.
