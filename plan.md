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

Release vs baseline (same news-rich grid, 6 runs per arm, artifacts report-ab-baseline vs report-ab-news-on): **0/6 decision flips**, prompt tokens flat (42,060 vs 41,998, -0.1%: the news trim and the manager's new weight paragraph cancel out), total tokens +8.3% and latency +0.8s/call in the release arm but both sit inside the batch-to-batch swing measured between identical configs elsewhere (+25% completion / 4.3-7.9s per call), so no release-driven speed or cost regression is detectable at n=1 batch per arm. The kill criteria (quality or speed drop) show no evidence of being tripped.

Bottom line: batch 2 is smaller than the plan hoped (trimming is ~1.5% of prompt tokens, cache saves provider calls not LLM cost, tiering waits on a second model); batch 3 is the substantive part (weights, turnover, overlay all proven by hermetic checks). Artifacts: backtests/report-ab-*.{json,md}.

## Merge readiness (2026-10-26)

**Divergence today**: main == origin/main == 7c14f1a, which is exactly the merge base. main has zero commits exp lacks, so merging now is conflict-free (main is a strict ancestor of exp). Every day both branches live, that stops being true.

**CI parity verified locally** (exactly what .github/workflows/ci.yml runs on a PR): `uv run ruff check app scripts` clean; `uv run test offline` = 34/34 groups green; the full suite including the browser smoke = 35/35 green at 07dd22b (main's suite has 32 groups).

**The case**:
- Dependency modernization: crewai -> openai-agents; the pydantic<2.13 pin workaround disappears with it; venv measured 959 -> 308 MB on the swap; uv.lock shrinks by ~3,000 lines; the pending fix/drop-lancedb PR (a crewai-dependency workaround) becomes obsolete and should be closed once this merges.
- Proven parity, not hoped: live A/B against the main-era code showed 3/3 decision parity, even speed, ~32% cheaper per run; release vs pre-release baseline showed 0/6 decision flips and flat prompt tokens.
- Verification depth: 35 check groups (vs 32), including new suites for the overlay, data cache, prompt trim, plus browser smoke.
- Real fixes main lacks: LLM_CONCURRENCY gate (the Modal 429 failure class), wash-trade pre-check (proven by the live ALAB incident), turnover cap, weight contract, risk overlay (off by default), TTL cache, honest backtest grading.
- Invariants intact: paper=True hardcoded, loopback host/origin guards, secret scan of the full diff clean (0 hits).
- Hygiene: reproducibility manifests, A/B artifacts, plan.md and memory trail; rollback is a main reset to 7c14f1a.

**Accepted caveats**: README results table still shows pre-batch-1 numbers and the wiki is untouched (both gated on owner go per edit; merging does not publish new numbers). Overlay is off by default. Model-tiering proof waits on a second model. A/B sample sizes are small; the case rests on verification, not on alpha claims.

**Mechanics when approved**: push exp, open PR exp -> main (origin/dev is fully contained in main, so the dev hop adds nothing today), rebase-and-merge keeps the 7-commit story, CI gates it. Nothing pushes or merges without the owner's word.

## Status

- [x] Batch 1 (items 1-6) - done 2026-10-26, all checks green, before/after table above
- [x] Batch 2 (items 7-10) - 7/10 pre-existing and verified; 8 and 9 shipped with live A/B evidence
- [x] Batch 3 (items 11-13) - shipped; hermetic coverage in check_automation, check_risk, check_overlay, check_calibration
- [x] Full suite: 35 check groups green (32 before + check_overlay, check_data_cache, check_prompt_trim)
- [ ] Live A/B re-run once a second model exists (tiering quality/speed proof)
- [ ] README update (owner go)
- [ ] Wiki update (owner go)
- [ ] Merge exp -> dev, PR dev -> main, release

---

# Phase 4: next increment (items 14-19)

Drafted 2026-10-26 after the FinRL-X re-comparison. Three batches, same rules as before: one batch per session, `uv run test all` green per item, live A/B only where it can actually answer the question. The single provable decision-quality item is 14; 17-18 are realism and protection; 15 makes every later change testable. Sequenced so each batch is independently shippable.

## Batch 4: stability and testability

**Item 14: manager self-consistency voting (the one that can improve decisions).**
The A/B exposed that 3 of 9 decisions flip between identical runs at temperature 0.2. Run the manager 3 times per ticker in parallel (LLM_CONCURRENCY already gates bursts), take the majority, and record `manager_votes` + `manager_agreement` (3/3, 2/1 split) on StockAnalysis, in the UI ("2/1 split" badge), and in reports. New setting MANAGER_VOTES (default 1 = today; 3 to enable). Ties resolve by summed confidence, then HOLD. Files: app/config.py, app/workflow.py, app/models.py, static/js/render.js, reports. Verification: hermetic check (scripted votes: majority, tie, agreement field) + live A/B on the news-rich grid: flips between identical configs at votes=3 vs the 3/9 baseline, cost delta (expect ~+2 manager calls per ticker, cents).

**Item 15: regression panel harness.**
Scripts `scripts/ab_panel.py --name x` runs one arm on a fixed grid (tickers/dates/depth/config in one place) and appends to `backtests/ab-ledger.json`; `--compare a b` prints the decision-flip, token, and cost table plus a same-config control arm when available. Every future change gets the same test the trim and release got, in one command. Verification: hermetic (stubbed LLM) plus one real ledger entry.

**Item 16: temperature study (experiment, may become a default).**
Run the fixed panel at LLM_TEMPERATURE=0 vs 0.2, two arm pairs plus controls. If flips collapse at 0, that is a free consistency win; if not, document and move on. No code unless the result says adopt a new default.

## Batch 5: realism and risk (deterministic, no LLM cost)

**Item 17: nonlinear fill costs in the backtest (FinRL-X PR #86 gap).**
Replace the flat 0.10% per positioned decision with base + participation + volatility slippage: `bp = base_bp + slope_bp * min(1, notional / avg_dollar_volume)` with a volatility multiplier, computed point-in-time from the snapshot's own volumes/closes (already cached). Report the effective cost per decision. Expect published numbers to move down on illiquid names; that is the point. Settings with sane defaults; PROMPT-free, no live cost. Verification: hand-computed hermetic cases + before/after report table.

**Item 18: deterministic regime gate (FinRL-X market_regime.py gap).**
New app/risk_regime.py: slow gate (SPX 26-week trend, 13-week drawdown, VIX robust z-score with a persistence requirement) + fast risk-off overlay (3-day price/vol shock). Output: a risk multiplier (1.0 / 0.75 / 0.5) and a cash-floor boost that the existing risk gate applies to BUY sizing; regime controls the risk budget, never the picks. Point-in-time by construction (computed from the same cached series), so backtests grade it honestly. Files: app/risk_regime.py, app/risk.py, app/workflow.py, app/config.py, settings. Verification: synthetic-series hermetic cases (risk-on, neutral, risk-off, fast-off expiry, persistence) + a mock backtest showing the multiplier binding.

## Batch 6: close the measurement loop

**Item 19: calibration-driven sizing.**
Feed the reliability curve back into the weight contract: scale the manager's proposed weight by `observed_hit_rate / predicted_probability` from the live calibration table (per probability band, gated at the minimum sample, clamped 0.5-1.0, flag in risk_flags when it binds). Small because the weight plumbing exists; converts batch 1's diagnostics into behavior. Verification: hermetic (seeded calibration rows) + flag/report surface.

## Ops items (owner actions, tracked here)

- [ ] Flip OVERLAY_ENABLED on and let the paper account run: only calendar time produces the drawdown comparison.
- [ ] Merge decision (readiness record above; zero-conflict today).
- [ ] README + wiki regeneration (owner go per edit; regenerate all numbers from fresh reports first).
- [ ] Tiering A/B (blocked on a second model on the endpoint).

## Phase 4 status

- [ ] Batch 4 (items 14-16): voting, regression panel, temperature study
- [ ] Batch 5 (items 17-18): fill costs, regime gate
- [ ] Batch 6 (item 19): calibration-driven sizing
