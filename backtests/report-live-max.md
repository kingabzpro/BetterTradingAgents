# Backtest report - llm mode

Generated 2026-10-26T13:51:31Z

| Setting | Value |
|---|---|
| Mode | llm (deepseek-ai/DeepSeek-V4.1-Flash) |
| LLM temperature | 0.0 |
| Tickers | NVDA, AMD, META, MSFT, AAPL, TSLA |
| Grid | 2026-03-02 to 2026-09-08 every 21d |
| Horizon | 21 days |
| Depth | max (excluded: market, fundamental) |
| Outlook | short_term |
| Fundamentals | excluded |
| Round-trip cost | base 0.10% + participation/volatility term (item 17) |
| Risk regime | off |
| Short selling | disabled (SELL is cash, graded on relative weakness vs SPY) |
| Portfolio accounting | equal weight per decision date, compounded |
| Benchmarks (buy & hold) | SPY +12.41% · QQQ +21.53% |
| LLM usage | 529,505 prompt + 183,420 completion = 712,925 tokens · ~$0.3790 across 60 priced run(s) |
| Decision policy | `2026-10-e` · debate rounds 2 |
| Manifest | `manifest-live-max.json` (revision, data hashes, seeds) |

## Flags

- window: **historical** - Headline performance claims belong to forward paper-trading windows (recent live runs on unresolved outcomes). Historical replays are qualitative evidence only.
- memorization risk: **high** - LLM-mode backtests on dates inside the model's training data are inflated: the model may already know how each window resolved. Prefer mock mode or very recent dates for headline numbers.
- fundamentals excluded from replay: opt in to the point-in-time SEC EDGAR source with --sec-edgar-fundamentals
- news items filtered to published <= decision date

## Results

Hit rate counts BUY and SELL calls; a long-only SELL is graded on relative weakness vs SPY (right when the stock underperforms), so SELLs are no longer invisible. Cumulative, Sharpe, and max drawdown compound the equal-weight per-date portfolio.

| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 10 | 3 / 4 / 3 | 42.90% | +0.43% | -0.93% | +4.30% | 1.24 | 0.64% | +25.71% |
| AMD | 10 | 2 / 2 / 6 | 75.00% | +3.91% | +7.65% | +39.35% | 1.42 | 0.00% | +206.05% |
| META | 10 | 0 / 6 / 4 | 50.00% | +0.00% | +1.07% | +0.00% | 0.0 | 0.00% | +9.78% |
| MSFT | 10 | 1 / 4 / 5 | 40.00% | -0.46% | -0.21% | -4.59% | -1.39 | 4.59% | +28.29% |
| AAPL | 10 | 2 / 2 / 6 | 75.00% | +1.90% | +3.35% | +19.91% | 2.04 | 0.00% | +28.06% |
| TSLA | 10 | 3 / 4 / 3 | 42.90% | -1.97% | -3.18% | -21.73% | -0.93 | 29.08% | -11.37% |
| overall | 60 | 11 / 22 / 27 | 51.50% | +0.64% | +0.63% | +6.17% | 0.99 | 4.39% | - |

## Manager probability calibration

`probability_beat_spy` scored against its frozen success event (the call's direction beats SPY over the horizon (alpha_pct > 0)); n=33 gradable positioned calls. Brier score: 0.2602 (lower is better; a constant 0.5 prediction scores 0.25).

| Predicted band | n | Mean predicted | Observed rate |
|---|---|---|---|
| 0.50 - 0.59 | 21 | 0.56 | Track record unavailable (n=21 < 30) |
| 0.60 - 0.69 | 12 | 0.62 | Track record unavailable (n=12 < 30) |

## Analyst scorecard

Each researcher's directional calls graded against SPY over the horizon (neutral signals are no-calls and stay out).

| Analyst | Directional calls | Hit rate | Mean alpha |
|---|---|---|---|
| bear | 60 | 36.70% | -2.64% |
| bull | 60 | 63.30% | +2.64% |
| forecast | 2 | 100.00% | +13.99% |
| judge | 52 | 42.30% | -1.68% |
| technical | 38 | 50.00% | +1.37% |

## Stop overlay replay

What the live stop rules (absolute -12.0%, trailing -15.0% from peak) would have done across the 11 graded BUY decisions in this sample: **2 exited early** (1 absolute, 1 trailing). Mean net delta vs holding to the horizon: +0.02% (sum +0.25%).

Daily closes only (triggers read at the close, conservative across gaps), no cooldown or re-entry modeling, same round-trip cost on both paths.

## Baselines (the pipeline must beat a cheap baseline to justify its cost)

| Baseline | Decisions | Positioned | Avg alpha | Cumulative | Note |
|---|---|---|---|---|---|
| all HOLD | 60 | - | - | 0.00% | every decision HOLD scores 0 net after costs |
| deterministic momentum | 60 | 24 | +0.19% | +2.89% | 63-day skip-month momentum, volatility-scaled (same costs) |
| buy & hold | per-ticker column above | - | - | per-ticker column | hold each ticker across the graded span |

## Decisions

| Ticker | Date | Decision | Conf. | Prob. | Weight | Entry | Exit | Cost | Net | SPY | Alpha | Regime |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AAPL | 2026-03-02 | HOLD | 0.50 | - | - | 264.25 | 251.04 | 0.10% | +0.00% | -4.26% | - | - |
| AMD | 2026-03-02 | SELL | 0.68 | 0.65 | 0.0% | 198.62 | 202.68 | 0.10% | +0.00% | -4.26% | -6.30% | - |
| META | 2026-03-02 | HOLD | 0.50 | - | - | 651.88 | 603.02 | 0.10% | +0.00% | -4.26% | - | - |
| MSFT | 2026-03-02 | SELL | 0.62 | 0.62 | 0.0% | 396.94 | 381.45 | 0.10% | +0.00% | -4.26% | -0.35% | - |
| NVDA | 2026-03-02 | HOLD | 0.50 | - | - | 182.05 | 175.24 | 0.10% | +0.00% | -4.26% | - | - |
| TSLA | 2026-03-02 | SELL | 0.60 | 0.58 | 0.0% | 403.32 | 380.85 | 0.10% | +0.00% | -4.26% | +1.32% | - |
| AAPL | 2026-03-23 | SELL | 0.62 | 0.58 | 0.0% | 251.04 | 258.74 | 0.10% | +0.00% | +4.69% | +1.62% | - |
| AMD | 2026-03-23 | HOLD | 0.55 | - | - | 202.68 | 246.83 | 0.10% | +0.00% | +4.69% | - | - |
| META | 2026-03-23 | SELL | 0.60 | 0.56 | 0.0% | 603.02 | 633.44 | 0.10% | +0.00% | +4.69% | -0.36% | - |
| MSFT | 2026-03-23 | SELL | 0.70 | 0.62 | 0.0% | 381.45 | 382.82 | 0.10% | +0.00% | +4.69% | +4.33% | - |
| NVDA | 2026-03-23 | SELL | 0.62 | 0.60 | 0.0% | 175.24 | 188.88 | 0.10% | +0.00% | +4.69% | -3.10% | - |
| TSLA | 2026-03-23 | SELL | 0.62 | 0.60 | 0.0% | 380.85 | 352.42 | 0.10% | +0.00% | +4.69% | +12.15% | - |
| AAPL | 2026-04-13 | HOLD | 0.55 | - | - | 258.74 | 276.34 | 0.10% | +0.00% | +4.65% | - | - |
| AMD | 2026-04-13 | BUY | 0.60 | 0.55 | 5.0% | 246.83 | 341.54 | 0.10% | +38.27% | +4.65% | +33.62% | - |
| META | 2026-04-13 | HOLD | 0.50 | - | - | 633.44 | 609.36 | 0.10% | +0.00% | +4.65% | - | - |
| MSFT | 2026-04-13 | HOLD | 0.50 | - | - | 382.82 | 411.95 | 0.10% | +0.00% | +4.65% | - | - |
| NVDA | 2026-04-13 | BUY | 0.58 | 0.55 | 8.0% | 188.88 | 198.03 | 0.10% | +4.74% | +4.65% | +0.09% | - |
| TSLA | 2026-04-13 | SELL | 0.68 | 0.60 | 0.0% | 352.42 | 392.51 | 0.10% | +0.00% | +4.65% | -6.72% | - |
| AAPL | 2026-05-04 | BUY | 0.60 | 0.55 | 5.0% | 276.34 | 308.06 | 0.10% | +11.38% | +4.54% | +6.84% | - |
| AMD | 2026-05-04 | HOLD | 0.55 | - | - | 341.54 | 503.89 | 0.10% | +0.00% | +4.54% | - | - |
| META | 2026-05-04 | SELL | 0.68 | 0.62 | 0.0% | 609.36 | 611.29 | 0.10% | +0.00% | +4.54% | +4.22% | - |
| MSFT | 2026-05-04 | HOLD | 0.55 | - | - | 411.95 | 415.25 | 0.10% | +0.00% | +4.54% | - | - |
| NVDA | 2026-05-04 | HOLD | 0.55 | - | - | 198.03 | 214.37 | 0.10% | +0.00% | +4.54% | - | - |
| TSLA | 2026-05-04 | BUY | 0.58 | 0.55 | 5.0% | 392.51 | 433.59 | 0.10% | +10.37% | +4.54% | +5.83% | - |
| AAPL | 2026-05-25 | HOLD | 0.55 | - | - | 308.55 | 296.16 | 0.10% | +0.00% | +1.23% | - | - |
| AMD | 2026-05-25 | HOLD | 0.55 | - | - | 467.51 | 547.26 | 0.10% | +0.00% | +1.23% | - | - |
| META | 2026-05-25 | SELL | 0.55 | 0.55 | 0.0% | 609.21 | 593.01 | 0.10% | +0.00% | +1.23% | +3.89% | - |
| MSFT | 2026-05-25 | BUY | 0.55 | 0.53 | 4.0% | 417.78 | 399.01 | 0.10% | -4.59% | +1.23% | -5.83% | - |
| NVDA | 2026-05-25 | HOLD | 0.50 | - | - | 214.84 | 212.21 | 0.10% | +0.00% | +1.23% | - | - |
| TSLA | 2026-05-25 | BUY | 0.60 | 0.55 | 3.0% | 426.01 | 411.15 | 0.10% | -3.59% | +1.23% | -4.82% | - |
| AAPL | 2026-06-15 | HOLD | 0.50 | - | - | 296.16 | 312.39 | 0.10% | +0.00% | -0.21% | - | - |
| AMD | 2026-06-15 | BUY | 0.55 | 0.55 | 3.0% | 547.26 | 552.05 | 0.10% | +0.78% | -0.21% | +0.99% | - |
| META | 2026-06-15 | SELL | 0.60 | 0.58 | 0.0% | 593.01 | 599.82 | 0.10% | +0.00% | -0.21% | -1.36% | - |
| MSFT | 2026-06-15 | SELL | 0.62 | 0.58 | 0.0% | 399.01 | 386.01 | 0.10% | +0.00% | -0.21% | +3.04% | - |
| NVDA | 2026-06-15 | SELL | 0.60 | 0.58 | 0.0% | 212.21 | 195.33 | 0.10% | +0.00% | -0.21% | +7.74% | - |
| TSLA | 2026-06-15 | HOLD | 0.55 | - | - | 411.15 | 419.77 | 0.10% | +0.00% | -0.21% | - | - |
| AAPL | 2026-07-06 | BUY | 0.60 | 0.57 | 8.0% | 312.39 | 336.62 | 0.10% | +7.66% | -1.62% | +9.28% | - |
| AMD | 2026-07-06 | HOLD | 0.55 | - | - | 552.05 | 494.95 | 0.10% | +0.00% | -1.62% | - | - |
| META | 2026-07-06 | HOLD | 0.50 | - | - | 599.82 | 593.40 | 0.10% | +0.00% | -1.62% | - | - |
| MSFT | 2026-07-06 | SELL | 0.60 | 0.58 | 0.0% | 386.01 | 388.37 | 0.10% | +0.00% | -1.62% | -2.23% | - |
| NVDA | 2026-07-06 | SELL | 0.62 | 0.60 | 0.0% | 195.33 | 196.29 | 0.10% | +0.00% | -1.62% | -2.11% | - |
| TSLA | 2026-07-06 | BUY | 0.58 | 0.54 | 4.0% | 419.77 | 309.22 | 0.10% | -26.44% | -1.62% | -24.81% | - |
| AAPL | 2026-07-27 | HOLD | 0.55 | - | - | 336.62 | 305.59 | 0.10% | +0.00% | +4.54% | - | - |
| AMD | 2026-07-27 | SELL | 0.68 | 0.65 | 0.0% | 494.95 | 506.00 | 0.10% | +0.00% | +4.54% | +2.31% | - |
| META | 2026-07-27 | SELL | 0.60 | 0.58 | 0.0% | 593.40 | 568.52 | 0.10% | +0.00% | +4.54% | +8.74% | - |
| MSFT | 2026-07-27 | HOLD | 0.50 | - | - | 388.37 | 479.45 | 0.10% | +0.00% | +4.54% | - | - |
| NVDA | 2026-07-27 | SELL | 0.62 | 0.60 | 0.0% | 196.29 | 224.76 | 0.10% | +0.00% | +4.54% | -9.96% | - |
| TSLA | 2026-07-27 | SELL | 0.78 | 0.65 | 0.0% | 309.22 | 339.30 | 0.10% | +0.00% | +4.54% | -5.18% | - |
| AAPL | 2026-08-17 | SELL | 0.62 | 0.58 | 0.0% | 305.59 | 316.22 | 0.10% | +0.00% | -0.87% | -4.35% | - |
| AMD | 2026-08-17 | HOLD | 0.55 | - | - | 506.00 | 505.74 | 0.10% | +0.00% | -0.87% | - | - |
| META | 2026-08-17 | SELL | 0.64 | 0.60 | 0.0% | 568.52 | 613.00 | 0.10% | +0.00% | -0.87% | -8.69% | - |
| MSFT | 2026-08-17 | HOLD | 0.55 | - | - | 479.45 | 493.95 | 0.10% | +0.00% | -0.87% | - | - |
| NVDA | 2026-08-17 | BUY | 0.58 | 0.55 | 5.0% | 224.76 | 225.48 | 0.10% | +0.22% | -0.87% | +1.09% | - |
| TSLA | 2026-08-17 | HOLD | 0.50 | - | - | 339.30 | 368.16 | 0.10% | +0.00% | -0.87% | - | - |
| AAPL | 2026-09-07 | HOLD | 0.50 | - | - | 319.97 | 338.40 | 0.10% | +0.00% | -0.35% | - | - |
| AMD | 2026-09-07 | HOLD | 0.55 | - | - | 477.57 | 607.87 | 0.10% | +0.00% | -0.35% | - | - |
| META | 2026-09-07 | HOLD | 0.55 | - | - | 616.28 | 715.62 | 0.10% | +0.00% | -0.35% | - | - |
| MSFT | 2026-09-07 | HOLD | 0.50 | - | - | 499.70 | 509.22 | 0.10% | +0.00% | -0.35% | - | - |
| NVDA | 2026-09-07 | BUY | 0.60 | 0.55 | 5.0% | 230.10 | 228.86 | 0.10% | -0.64% | -0.35% | -0.29% | - |
| TSLA | 2026-09-07 | HOLD | 0.55 | - | - | 354.08 | 357.45 | 0.10% | +0.00% | -0.35% | - | - |

---
Educational project - these numbers describe a simulation of a rule-based or LLM pipeline, not investment advice.
