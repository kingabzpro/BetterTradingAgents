# BetterTradingAgents: Next Release Plan

Created 2026-10-25. Branch: `exp/openai-agents-sdk` (local-only). Goal: ship all items below as the next release. Release path when approved: `exp/openai-agents-sdk` -> `dev` -> PR to `main`. Nothing merges until the owner approves (standing rule: quality or speed drop = delete the branch).

START HERE: work top to bottom. One batch per session, each ends with `uv run test all` green.

## Where these ideas come from

- Repo: [FinRL-X](https://github.com/AI4Finance-Foundation/FinRL-Trading) (`AI4Finance-Foundation/FinRL-Trading`, 3.9k stars, Apache-2.0), the production-oriented successor to FinRL.
- Research paper: **"FinRL-X: An AI-Native Modular Infrastructure for Quantitative Trading"**, [arXiv:2603.21330](https://arxiv.org/abs/2603.21330) (PAKDD 2026, DMO-FinTech workshop; Yang, Zhang, She, Liao, Zhang).
- Borrowed concepts: weight-centric contract, mechanical risk overlay (stop-loss/trailing/cooldown), two-layer market regime gate, pre-trade execution guards, multi-benchmark backtest with cost models.
- A local clone was inspected at `C:\WINDOWS\TEMP\finrl-x` (temp dir, may be gone; re-clone if needed).

## Batch 1: Accuracy + backtest honesty (~1 session, offline verifiable)

1. **SELL relative grading** in `app/backtest/grade.py`: long-only mode currently grades SELL as flat 0, so a third of the decision space is unmeasured. Grade a SELL on whether the stock underperforms SPY over the horizon (no shorting simulation).
2. **Confidence calibration**: bucket `confidence` and `probability_beat_spy` (manager already emits both) against realized outcomes; Brier score; persist per run.
3. **QQQ benchmark** alongside SPY in backtest reports (picks are tech-heavy; SPY alone is a weak bar).
4. **Equal-weight portfolio accounting** in the harness: replace the cash-except-BUY basis so reported numbers cannot flip depending on interpretation.
5. **Per-analyst scorecard**: hit rate per agent (technical, news, sentiment, ...) over rolling windows, persisted, surfaced to the judge.
6. **Forward-window labeling**: forward paper windows carry the headline claims; historical LLM backtests labeled as memorization-risky.

## Batch 2: Speed (~1 session)

7. **Model tiering**: cheap/fast model for the 8 analysts, strongest model for judge + manager. New layered setting in `app/config.py`. Biggest single speed and cost win.
8. **Dossier trimming**: cap news items, truncate filings, drop duplicated indicator text.
9. **TTL cache for live news/prices** (~15 min default); the backtest has a snapshot cache, live runs re-fetch.
10. **Per-agent progress streaming** to the UI (sinks already exist per agent run).

## Batch 3: Contract + safety (~1 session, riskiest, budget slack)

11. **Weight-centric contract**: manager emits a target weight (0-100%) per ticker; the paper executor and the backtest harness consume the same object. Touches manager schema, `app/workflow.py`, broker executor, backtest, UI.
12. **Pre-trade guards**: max order value + max turnover per rebalance, scale orders down proportionally when over (pattern from FinRL-X `trade_executor.py`).
13. **Risk overlay between agent runs**: trailing stop + absolute stop + cooldown, checked daily; agents decide, the overlay protects. Zero LLM cost.

## Verification

- Every batch: `uv run test all` green before moving on.
- Live A/B sweep for tiering (7) and trimming (8): roughly $3-5 direct against DeepSeek. **Blocker: Modal key 401s since 2026-10-25**; owner fixes the key or sweep runs via direct API. Mock checks prove correctness either way; the live sweep proves no quality drop.

## After implementation: docs (explicit owner go required per edit)

- **README**: update features and results sections. Standing rule: never touch the README results section without an explicit per-edit request from the owner.
- **Wiki**: rewrite `Backtesting.md` results + add pages for the risk overlay and the weight contract. Wiki updates also need explicit owner go.
- **All published metrics change**: SELL relative grading + equal-weight accounting alter every reported number (hit rates, cumulative returns, the $2.34 live-sweep tables). Regenerate tables from fresh runs before any doc edit.

## Batch 1 outcome (2026-10-26, committed on exp/openai-agents-sdk)

Shipped items 1-6. Same mock run (6 tickers, 2025-06-02 to 2026-09-14, 138 decisions) before vs after, artifacts in `backtests/report-batch1-{before,after}.{json,md}`:

| Metric | Before (old basis) | After (new basis) |
|---|---|---|
| Decisions / counts | 138, 89 BUY / 25 SELL / 24 HOLD | identical (only grading changed) |
| Hit rate | 58.4% (SELLs invisible) | 54.4% (SELLs graded vs SPY, 25 calls now counted) |
| Avg alpha | +2.29% (BUYs only) | +1.43% (BUYs + relative SELL alphas) |
| Cumulative | +1171.15% (100%-weight per BUY) | +60.73% (equal-weight per date) |
| Sharpe / Max DD | 1.07 / 60.18% | 2.03 / 13.74% |
| Benchmarks | SPY +31.31% only | SPY +31.31% / QQQ +41.76% |

Also new in every report: manager-probability reliability table (Brier; the rule-based mock scores 0.4209, predicted 0.95 vs observed 53.5% - exactly the overconfidence the table exists to expose), per-analyst scorecard (technical 53.5% hit, n=114), equal-weight accounting note, and forward/historical window labels. The judge now receives a live analyst track record (SQLite join of decisions + stored runs, 90-day window, min-sample gated) and refuses it in replays/incognito.

All 32 check groups green after the change (`uv run test all`). Items 7-13 unchanged from the plan below.

## Status

- [x] Batch 1 (items 1-6) - done 2026-10-26, all checks green, before/after table above
- [ ] Batch 2 (items 7-10)
- [ ] Batch 3 (items 11-13)
- [ ] Live A/B verification sweep (still blocked on the Modal key; direct DeepSeek fallback ~$3-5)
- [ ] README update (owner go)
- [ ] Wiki update (owner go)
- [ ] Merge exp -> dev, PR dev -> main, release
