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

## Batch 2+3 outcome (2026-10-26, second session)

Items 7-13 done. Items 7 (per-role/per-agent model overrides) and 10 (per-agent SSE progress: agent_started/token/completed) already existed in the codebase and are covered by check_llm_reliability and check_streaming; the Modal endpoint serves exactly one model, so a live tiering A/B is impossible until a second model exists (flip LLM_MODEL_ANALYSTS on the Settings page when there is one). What shipped this session:

- **Item 8, prompt trim**: news payloads capped at 5 items / 240-char summaries / 800-char article content; social capped at 8. PROMPT_TRIM=0 restores the old payloads. Measured on real cached news: payload -18%, prompt tokens -626 over 6 pro runs (-1.5%), decisions 5/6 identical.
- **Item 9, live TTL cache**: news/social responses reused for 15 min, history windows for 5 min (NEWS_CACHE_TTL_SECONDS / HISTORY_CACHE_TTL_SECONDS, 0 = off). Proof: two analyses of one ticker fetch news 1x instead of 2x (check_data_cache).
- **Item 11, weight contract**: the manager proposes target_weight_pct (0-100, null for HOLD/SELL), the risk gate caps size by it (never raises it), the final gated weight rides on StockAnalysis and sizes autopilot orders (weight x equity), the UI shows it, and backtest reports carry a Weight column. A SELL targets 0%.
- **Item 12, turnover guard**: one autopilot session may not propose more than MAX_TURNOVER_PCT (default 50%) of equity in turnover; over it, buys scale down proportionally (sub-$1 buys dropped with a reason), sells are exits and are never scaled but consume budget first.
- **Item 13, risk overlay**: app/overlay.py exits positions past the absolute stop (-12% default) or trailing stop (-15% from peak) via broker.submit_exit_order (run_id "overlay", reason recorded), sets a 5-day re-entry cooldown that autopilot honors, and never wedges a caller. **OFF by default** (flip it on the Settings page); the numbers above are the defaults to edit there too.
- **Reports**: every LLM backtest now reports token totals and priced cost (llm_usage in the JSON, a row in the markdown).

### A/B evidence (live, real DeepSeek via Modal, ~$0.30 total)

June grid (NVDA/AMD/MSFT x 3 dates, pro): 3/9 decision flips between trim on/off, but the same-config control also flipped 3/9 - single-run decision parity on this grid is noise-dominated (temperature 0.2), and that grid's cached news was empty, so the trim had nothing to act on there.

News-rich grid (NVDA/AMD, 2026-08/09): prompt tokens -626 (-1.5%) with trim, total tokens within +1.1% (completion/reasoning length variance dominates), 1/6 flips. Durations: batch variance, no consistent direction.

Bottom line: batch 2 is smaller than the plan hoped (trimming is ~1.5% of prompt tokens, cache saves provider calls not LLM cost, tiering waits on a second model); batch 3 is the substantive part (weights, turnover, overlay all proven by hermetic checks). Artifacts: backtests/report-ab-*.{json,md}.

## Status

- [x] Batch 1 (items 1-6) - done 2026-10-26, all checks green, before/after table above
- [x] Batch 2 (items 7-10) - 7/10 pre-existing and verified; 8 and 9 shipped with live A/B evidence
- [x] Batch 3 (items 11-13) - shipped; hermetic coverage in check_automation, check_risk, check_overlay, check_calibration
- [x] Full suite: 35 check groups green (32 before + check_overlay, check_data_cache, check_prompt_trim)
- [ ] Live A/B re-run once a second model exists (tiering quality/speed proof)
- [ ] README update (owner go)
- [ ] Wiki update (owner go)
- [ ] Merge exp -> dev, PR dev -> main, release
