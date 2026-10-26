# Backtest report - llm mode

Generated 2026-10-26T10:06:16Z

| Setting | Value |
|---|---|
| Mode | llm (deepseek-ai/DeepSeek-V4.1-Flash) |
| Tickers | NVDA, AMD, MSFT |
| Grid | 2025-06-02 to 2025-07-14 every 21d |
| Horizon | 21 days |
| Depth | pro (excluded: market) |
| Outlook | short_term |
| Fundamentals | excluded |
| Round-trip cost | 0.10% |
| Short selling | disabled (SELL is cash, graded on relative weakness vs SPY) |
| Portfolio accounting | equal weight per decision date, compounded |
| Benchmarks (buy & hold) | SPY +6.80% · QQQ +7.94% |
| LLM usage | 105,913 prompt + 81,887 completion = 187,800 tokens · ~$0.1300 across 9 priced run(s) |
| Decision policy | `2026-10-e` · debate rounds 2 |
| Manifest | `manifest-votes3-b.json` (revision, data hashes, seeds) |

## Flags

- window: **historical** - Headline performance claims belong to forward paper-trading windows (recent live runs on unresolved outcomes). Historical replays are qualitative evidence only.
- memorization risk: **high** - LLM-mode backtests on dates inside the model's training data are inflated: the model may already know how each window resolved. Prefer mock mode or very recent dates for headline numbers.
- fundamentals excluded from replay: opt in to the point-in-time SEC EDGAR source with --sec-edgar-fundamentals
- news items filtered to published <= decision date

## Results

Hit rate counts BUY and SELL calls; a long-only SELL is graded on relative weakness vs SPY (right when the stock underperforms), so SELLs are no longer invisible. Cumulative, Sharpe, and max drawdown compound the equal-weight per-date portfolio.

| Scope | Decisions | BUY / SELL / HOLD | Hit rate | Avg net | Avg alpha | Cumulative | Sharpe | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 3 | 3 / 0 / 0 | 100.00% | +9.39% | +7.16% | +30.67% | 10.82 | 0.00% | +31.03% |
| AMD | 3 | 2 / 0 / 1 | 100.00% | +8.57% | +10.02% | +27.35% | 5.9 | 0.00% | +54.22% |
| MSFT | 3 | 1 / 0 / 2 | 100.00% | +2.13% | +5.37% | +6.38% | 2.95 | 0.00% | +15.95% |
| overall | 9 | 6 / 0 / 3 | 100.00% | +6.69% | +7.82% | +21.42% | 18.32 | 0.00% | - |

## Manager probability calibration

`probability_beat_spy` scored against its frozen success event (the call's direction beats SPY over the horizon (alpha_pct > 0)); n=6 gradable positioned calls. Brier score: 0.2041 (lower is better; a constant 0.5 prediction scores 0.25).

| Predicted band | n | Mean predicted | Observed rate |
|---|---|---|---|
| 0.50 - 0.59 | 6 | 0.55 | Track record unavailable (n=6 < 30) |

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

| Ticker | Date | Decision | Conf. | Prob. | Weight | Entry | Exit | Net | SPY | Alpha |
|---|---|---|---|---|---|---|---|---|---|---|
| AMD | 2025-06-02 | BUY | 0.60 | 0.55 | 6.0% | 114.63 | 129.58 | +12.94% | +1.55% | +11.39% |
| MSFT | 2025-06-02 | HOLD | 0.58 | - | - | 457.45 | 481.24 | +0.00% | +1.55% | - |
| NVDA | 2025-06-02 | BUY | 0.60 | 0.56 | 5.0% | 137.03 | 143.82 | +4.85% | +1.55% | +3.30% |
| AMD | 2025-06-23 | BUY | 0.58 | 0.55 | 4.0% | 129.58 | 146.24 | +12.76% | +4.11% | +8.65% |
| MSFT | 2025-06-23 | HOLD | 0.55 | - | - | 481.24 | 498.10 | +0.00% | +4.11% | - |
| NVDA | 2025-06-23 | BUY | 0.60 | 0.55 | 6.0% | 143.82 | 163.67 | +13.70% | +4.11% | +9.59% |
| AMD | 2025-07-14 | HOLD | 0.56 | - | - | 146.24 | 176.78 | +0.00% | +1.02% | - |
| MSFT | 2025-07-14 | BUY | 0.58 | 0.53 | 5.0% | 498.10 | 530.40 | +6.38% | +1.02% | +5.37% |
| NVDA | 2025-07-14 | BUY | 0.60 | 0.55 | 3.0% | 163.67 | 179.56 | +9.61% | +1.02% | +8.59% |

---
Educational project - these numbers describe a simulation of a rule-based or LLM pipeline, not investment advice.
