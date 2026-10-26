# Backtest report - llm mode

Generated 2026-10-26T09:04:34Z

| Setting | Value |
|---|---|
| Mode | llm (deepseek-ai/DeepSeek-V4.1-Flash) |
| Tickers | NVDA, AMD |
| Grid | 2026-08-03 to 2026-09-14 every 21d |
| Horizon | 21 days |
| Depth | pro (excluded: market) |
| Outlook | short_term |
| Fundamentals | excluded |
| Round-trip cost | 0.10% |
| Short selling | disabled (SELL is cash, graded on relative weakness vs SPY) |
| Portfolio accounting | equal weight per decision date, compounded |
| Benchmarks (buy & hold) | SPY +2.52% · QQQ +8.13% |
| LLM usage | 41,998 prompt + 18,814 completion = 60,812 tokens · ~$0.0352 across 6 priced run(s) |
| Decision policy | `2026-10-e` · debate rounds 2 |
| Manifest | `manifest-ab-news-on.json` (revision, data hashes, seeds) |

## Flags

- window: **forward** - Headline performance claims belong to forward paper-trading windows (recent live runs on unresolved outcomes). Historical replays are qualitative evidence only.
- memorization risk: **reduced (forward window - recent dates)** - LLM-mode backtests on dates inside the model's training data are inflated: the model may already know how each window resolved. Prefer mock mode or very recent dates for headline numbers.
- fundamentals excluded from replay: opt in to the point-in-time SEC EDGAR source with --sec-edgar-fundamentals
- news items filtered to published <= decision date

## Results

Hit rate counts BUY and SELL calls; a long-only SELL is graded on relative weakness vs SPY (right when the stock underperforms), so SELLs are no longer invisible. Cumulative, Sharpe, and max drawdown compound the equal-weight per-date portfolio.

| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 3 | 0 / 2 / 1 | 0.00% | +0.00% | -6.40% | +0.00% | 0.0 | 0.00% | +15.74% |
| AMD | 3 | 0 / 3 / 0 | 33.30% | +0.00% | -9.27% | +0.00% | 0.0 | 0.00% | +30.35% |
| overall | 6 | 0 / 5 / 1 | 20.00% | +0.00% | -8.12% | +0.00% | 0.0 | 0.00% | - |

## Manager probability calibration

`probability_beat_spy` scored against its frozen success event (the call's direction beats SPY over the horizon (alpha_pct > 0)); n=5 gradable positioned calls. Brier score: 0.3140 (lower is better; a constant 0.5 prediction scores 0.25).

| Predicted band | n | Mean predicted | Observed rate |
|---|---|---|---|
| 0.50 - 0.59 | 4 | 0.58 | Track record unavailable (n=4 < 30) |
| 0.60 - 0.69 | 1 | 0.62 | Track record unavailable (n=1 < 30) |

## Analyst scorecard

Each researcher's directional calls graded against SPY over the horizon (neutral signals are no-calls and stay out).

| Analyst | Directional calls | Hit rate | Mean alpha |
|---|---|---|---|
| bear | 6 | 16.70% | -6.79% |
| bull | 6 | 83.30% | +6.79% |
| technical | 4 | 25.00% | -3.66% |

## Baselines (the pipeline must beat a cheap baseline to justify its cost)

| Baseline | Decisions | Positioned | Avg alpha | Cumulative | Note |
|---|---|---|---|---|---|
| all HOLD | 6 | - | - | 0.00% | every decision HOLD scores 0 net after costs |
| deterministic momentum | 6 | 5 | +5.81% | +16.16% | 63-day skip-month momentum, volatility-scaled (same costs) |
| buy & hold | per-ticker column above | - | - | per-ticker column | hold each ticker across the graded span |

## Decisions

| Ticker | Date | Decision | Conf. | Prob. | Weight | Entry | Exit | Net | SPY | Alpha |
|---|---|---|---|---|---|---|---|---|---|---|
| AMD | 2026-08-03 | SELL | 0.62 | 0.58 | 0.0% | 484.64 | 456.75 | +0.00% | +0.77% | +6.52% |
| NVDA | 2026-08-03 | HOLD | 0.55 | - | - | 206.41 | 208.25 | +0.00% | +0.77% | - |
| AMD | 2026-08-24 | SELL | 0.72 | 0.62 | 0.0% | 456.75 | 493.41 | +0.00% | -0.34% | -8.37% |
| NVDA | 2026-08-24 | SELL | 0.60 | 0.58 | 0.0% | 208.25 | 210.96 | +0.00% | -0.34% | -1.64% |
| AMD | 2026-09-14 | SELL | 0.62 | 0.58 | 0.0% | 493.41 | 631.75 | +0.00% | +2.09% | -25.95% |
| NVDA | 2026-09-14 | SELL | 0.62 | 0.58 | 0.0% | 210.96 | 238.90 | +0.00% | +2.09% | -11.16% |

---
Educational project - these numbers describe a simulation of a rule-based or LLM pipeline, not investment advice.
