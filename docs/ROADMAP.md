# BetterTradingAgents — Development Roadmap

Research-backed implementation plans for everything after the current quick wins.
Each item lists the evidence behind it, the concrete design in this codebase, and
acceptance criteria. Last updated: 2026-09-02.

**Already shipped** (this is the baseline the plans build on):

- Technical indicators: MACD, Bollinger percent_b, ATR, volume/relative-volume
- Portfolio: close positions, realized P&L, trade history, cash-balance guard
- Portfolio Manager sees current holdings (`current_portfolio` in its dossier)
- Old SQLite schemas migrate automatically via `ALTER TABLE`
- **1.2 Risk layer + sizing**: deterministic gate in `app/risk.py` — BUYs get a
  vol-scaled size (`default × confidence × min(1, 15/vol)`, floored at 0.25×) and
  are downgraded to HOLD when they would breach `MAX_POSITION_PCT` (10%),
  `MAX_INVESTED_PCT` (60%) or `MIN_CASH_PCT` (10%); 2+ failed analysts cap
  confidence at 0.5; results carry `suggested_size_usd` + `risk_flags`
- **2.1 Rebuttal round**: `DEBATE_ROUNDS >= 2` runs a second bull/bear exchange
  where each side answers the other; the manager sees the full transcript and the
  final positions are the post-rebuttal ones

---

## Phase 1 — Learn from outcomes, size the risk

### 1.1 Decision memory with realized-return reflection

**Why.** TradingAgents attributes much of its edge to exactly this mechanism: every
completed run appends its decision to a memory log; on the next run of the same
ticker the framework fetches the realized return (raw and alpha vs SPY), generates
a one-paragraph reflection, and injects recent same-ticker decisions plus
cross-ticker lessons into the Portfolio Manager prompt ([TradingAgents repo](https://github.com/TauricResearch/TradingAgents),
[arXiv 2412.20138](https://arxiv.org/html/2412.20138v5)). Layered memory with decay
is the recurring theme across FinMem (3-level memory, working → short-term →
long-term with promotion/decay, [arXiv 2311.13743](https://arxiv.org/abs/2311.13743))
and TradingGPT (3 layers, custom decay per layer, [arXiv 2309.03736](https://www.alphaxiv.org/abs/2309.03736)).

**Design.**

- New SQLite table `decisions` (reuse `portfolio.db`): `run_id, ticker, date, decision,
  confidence, price_at_decision, summary, bull_case, bear_case`. Written at the end of
  `analyze_ticker` in `app/workflow.py`.
- New module `app/memory.py`:
  - `record_decision(analysis, price)` — append after each run.
  - `get_reflections(ticker) -> list[dict]` — for each past decision older than
    N days (start with 21), compute realized return and alpha vs SPY from yfinance
    (`_yf_all`-style helper with `start/end` dates; SPY closes come free from the
    same call). Store the computed outcome back on the row so it's computed once.
  - Reflection text: in mock mode a deterministic sentence; with an LLM, one extra
    single-agent crew ("given decision X at price Y and realized return Z vs SPY,
    write a 2-sentence lesson").
- Inject into the manager dossier in `app/workflow.py` as
  `"past_decisions": [...]` (most recent 3 for the ticker + 2 cross-ticker lessons),
  mirroring the existing `current_portfolio` pattern.
- Decay: only the most recent K reflections are injected (start K=5). Full FinMem-style
  layered promotion is out of scope for v1; the DB keeps everything for the backtester.

**Acceptance.** Second run of the same ticker shows non-empty `past_decisions` with a
computed `realized_return_pct` and `alpha_vs_spy_pct`; a decision row exists for every
completed `ticker_completed`; portfolio/analysis failures never block a run (same
try/except pattern as `_portfolio_snapshot`).

**Effort:** M (1–2 days). **Risk:** yfinance history lookups add latency — cache
outcome computation on the decisions row; reflection LLM call adds cost — make it
optional behind `MEMORY_REFLECT_WITH_LLM` (default off, deterministic sentence).

### 1.2 Risk layer + position sizing

**Why.** TradingAgents uses a dedicated risk-management team whose reports the
Portfolio Manager must weigh ([arXiv 2412.20138](https://arxiv.org/html/2412.20138v5)).
FinCon's risk controller uses CVaR and rewrites analyst prompts from realized
performance ([emergentmind overview](https://www.emergentmind.com/topics/multi-agent-llm-financial-trading)).
On sizing, dollar-volatility parity is the standard robust default — size each
position so its dollar volatility is equal (a 10%-vol stock gets twice the dollar
size of a 20%-vol stock) ([QuanterLab](https://quanterlab.com/articles/foundations-position-sizing)),
and the Kelly fraction `f = μ/σ²` justifies scaling size linearly with conviction
and inversely with variance; fractional Kelly is the practical form
([Breaking Alpha summary of MacLean et al.](https://breakingalpha.io/insights/position-sizing-algorithmic-trading)).

**Design.**

- Deterministic risk gate in `app/risk.py` (rules, not an LLM — free, testable,
  no hallucination surface). Runs after the manager decides, before the result is
  emitted:
  1. **Position size**: `size_usd = DEFAULT_POSITION_SIZE × confidence × min(1, 15 / vol_ann)`
     where `vol_ann` is the already-computed `volatility_annualized_pct`. Clamped to
     `[0.25×, 1.5×]` of the default. Attached to the result as `suggested_size_usd`
     (dollar-vol parity scaled by conviction).
  2. **Exposure caps**: max 10% of equity per ticker, max 60% invested overall,
     min 10% cash buffer — computed from `portfolio.get_portfolio()`. A BUY that
     breaches a cap is downgraded to HOLD with an explicit reason string.
  3. **Missing-input brake**: if 2+ of the three analyst slots failed, cap
     confidence at 0.5 and note it (currently only a total manager failure degrades
     the decision).
- New fields on `ManagerResult`/`StockAnalysis`: `suggested_size_usd`,
  `risk_flags: list[str]` (e.g. `"downgraded: position would exceed 10% of equity"`).
- UI: show size + flags on the result card; "Add to Demo Portfolio" uses the
  suggested size instead of `DEFAULT_POSITION_SIZE`.
- Optional v2 (only if the backtester from 2.2 proves value): an LLM risk agent in
  the loop, FinCon-style, fed CVaR of the current book.

**Acceptance.** Unit tests: size formula cases (high/low vol, confidence clamp),
cap downgrades, missing-input brake. A BUY on an already-10% holding returns HOLD
with `risk_flags` explaining why.

**Effort:** M. **Risk:** over-restrictive caps make everything HOLD — expose caps
as env settings (`MAX_POSITION_PCT`, `MAX_INVESTED_PCT`, `MIN_CASH_PCT`) and tune
with the backtester.

---

## Phase 2 — Better reasoning, measurable results

### 2.1 Rebuttal round in the bull/bear debate

**Why.** Today bull and bear run in parallel on identical inputs and never see each
other (`app/workflow.py` stage 2) — it's two monologues, not a debate. Multi-agent
debate where agents read and critique each other converges on better answers
(Du et al.: accuracy rose with rounds, diminishing returns after ~3;
[arXiv 2305.14325](https://www.alphaxiv.org/abs/2305.14325)). But each extra round
grows context and cost ([ACL 2026](https://aclanthology.org/2026.acl-srw.1.pdf)),
and TradingAgents itself uses a small fixed number of rounds. One rebuttal round
(2 total) is the sweet spot for a 6-agent budget.

**Design.**

- After the parallel bull/bear stage, run a second cheap pass per side with a
  modified task: each gets the other side's first-round summary and must
  (a) rebut the strongest opposing point and (b) concede or restate its own score.
- New `build_rebuttal_task` in `app/agents/bull.py` / `bear.py` (same output schema,
  extra field `rebuttal`), new `rebuttal` payload key. Skip if the first round for
  either side failed (degrade exactly like stage 1 does).
- The manager dossier gets both rounds; the SSE event stream gets
  `agent_started/completed` events for `bull_rebuttal`/`bear_rebuttal` so the UI
  shows the extra stage live.
- Config: `DEBATE_ROUNDS` (default 2, hard cap 3) so it can be turned off.

**Acceptance.** A run with `DEBATE_ROUNDS=2` emits 4 stage-2 agent events; results
include rebuttal text; failure of a rebuttal pass never fails the run; A/B compare
on 20 ticker-runs shows the manager citing rebuttals (spot check).

**Effort:** S–M. **Risk:** +2 LLM calls per ticker (~33% cost increase) — default
on, but documented; mock mode needs a deterministic rebuttal stub.

### 2.2 Backtesting & evaluation harness

**Why.** Nothing in the repo can answer "is this better than buy-and-hold?". The
literature is unambiguous about the two traps: (1) classic look-ahead/data-snooping
bias — validate walk-forward with point-in-time data only
([arXiv 2512.12924](https://arxiv.org/html/2512.12924v1)); (2) an LLM-specific trap —
the model's *training data already contains historical outcomes*, so backtests on
dates the model memorized are inflated ([paperswithbacktest](https://paperswithbacktest.com/course/look-ahead-bias-llm-trading)).
Standard metrics: cumulative return, Sharpe, max drawdown, win rate vs baseline
(the same set TradingAgents and the [LLM+RL trading papers](https://arxiv.org/html/2502.01574v1) report).

**Design.**

- `app/backtest/` package, CLI entry `uv run python -m app.backtest --tickers NVDA,AMD --start 2024-01-01 --end 2025-06-30 --step 21d`:
  - For each date T in the walk-forward grid, build `MarketData` **as-of T**
    (`yf.download(start=T-6mo, end=T)` — extend `_yf_all` with date bounds) and run
    the agent pipeline (mock mode counts as the cheap first baseline).
  - Grade: BUY → long T→T+21d return, SELL → short (or 0 for long-only mode),
    HOLD → 0; subtract `2×5bp` round-trip cost; compare vs SPY same window.
  - Aggregate: hit rate per decision type, cumulative return, Sharpe, max drawdown,
    buy-and-hold baseline. Write JSON + markdown report to `docs/backtests/`.
- **Anti-look-ahead rules, enforced in code**: (a) news items filtered to
  `published <= T`; (b) fundamentals are current-vintage — acceptable known bias,
  must be stated in the report; (c) LLM-mode backtests on dates < model knowledge
  cutoff are flagged `memorization_risk: high` in the report; prefer mock mode or
  recent dates for headline numbers.
- Data caching layer (`app/backtest/cache.py`, SQLite keyed by ticker+date) so
  re-runs don't re-fetch yfinance.
- Weekly local job (not CI — network + cost): `scripts/backtest_smoke.py` runs
  3 tickers × 6 dates and regenerates the report.

**Acceptance.** One completed report exists in `docs/backtests/` for ≥3 tickers;
grading unit-tested against hand-computed windows; re-running with a warm cache
makes zero network calls; report includes the memorization-risk flag when LLM mode
is used.

**Effort:** L (3–5 days). **Risk:** LLM backtests cost real money — gate LLM mode
behind `--llm` with an explicit cost estimate printed first; default is mock mode.

### 2.3 LLM reliability & per-agent models

**Why.** Production CrewAI guidance: pass validated structured outputs between
tasks, validate early, retry with the *error* fed back
([CrewAI production lessons](https://www.agilesoftlabs.com/blog/2026/06/crewai-in-production-2026-real-lessons)).
The current single blind retry (`app/workflow.py`) ignores why a call failed.

**Design.**

- Retry v2: on `ValidationError` from `to_result`/`extract_json`, retry once with
  the validation message appended to the task ("your previous output was rejected:
  <error>; return ONLY corrected JSON"). On HTTP 429/5xx, exponential backoff
  (1s, 4s). Distinguish the two in logs.
- `response_format={"type": "json_object"}` on agents when the provider supports it
  (feature-detect via a one-time `/models` or first-call probe; silently skip
  unsupported providers).
- Per-agent overrides (README roadmap item): env keys `LLM_MODEL_MANAGER`,
  `LLM_MODEL_ANALYSTS`, `LLM_MODEL_DEBATE` falling back to `LLM_MODEL`; build one
  LLM per role in `get_llm()`'s cache. Rationale: cheap fast models for analysts,
  stronger model only for the manager.
- Token/cost accounting: read `usage_metrics` from CrewAI outputs, sum per run,
  include in `ticker_completed` event and the results JSON.

**Acceptance.** Unit test feeds a malformed payload through the retry path with a
stubbed LLM and asserts the corrective second call happened; mixed-model config
produces different LLM objects per role; run summary shows token totals.

**Effort:** M. **Risk:** provider-specific `response_format` quirks — always keep
`extract_json` as the fallback parser.

---

## Phase 3 — Breadth & experience

### 3.1 Sentiment / social analyst (4th researcher)

**Why.** TradingAgents' analyst team includes a sentiment analyst reading social
media; StockTwits/Reddit sentiment demonstrably predicts short-horizon returns for
retail-driven names ([FinBERT + StockTwits study](https://pmc.ncbi.nlm.nih.gov/articles/PMC10280432)).

**Design.** Clone the `news.py` pattern: `app/agents/sentiment.py`, data from an
Olostep query `"{ticker} (site:reddit.com OR site:stocktwits.com)"`, keyword/LLM
sentiment scoring in the same `AnalystResult` schema, runs in the stage-1
`asyncio.gather`. Mock mode: the existing positive/negative word lists. Show as a
4th column in the results UI. Flag when social volume is too thin to mean anything
(< N results → neutral with low confidence).

**Effort:** S–M.

### 3.2 Stream agent reasoning live (README roadmap)

**Why.** Users watch a spinner for ~30–60 s per agent; streaming tokens is the
standard fix and the SSE plumbing already exists.

**Design.** CrewAI supports token-level callbacks on the LLM. Wire a per-agent
callback that pushes `agent_token` SSE events with a sliding buffer (cap ~2 KB per
agent, truncate summaries server-side), render a collapsible live-reasoning pane
per agent in `index.html`. Mock mode streams the deterministic text word-by-word so
the UI path is always testable.

**Effort:** M.

### 3.3 Researcher-configurable debate depth

Only after 2.1 + 2.2 exist: use the backtester to compare `DEBATE_ROUNDS` 1 vs 2
vs 3 on the same grid and pick the default from data, following the diminishing-
returns curve in [Du et al.](https://www.alphaxiv.org/abs/2305.14325) rather than
guessing. **Effort:** S once the harness exists.

---

## Phase 4 — Execution (explicit opt-in)

### 4.1 Alpaca paper trading (README roadmap)

**Why.** The current SQLite portfolio is manual; Alpaca's paper API gives real
order semantics (fills, partials, slippage) with zero real-money risk, and it's
the README's own roadmap item.

**Design.** `app/brokers/alpaca.py` behind a `Broker` protocol (`submit(order)`,
`positions()`, `price(ticker)`) with the SQLite portfolio as the default
implementation; `BROKER=alpaca|local` env switch; the risk layer's
`suggested_size_usd` becomes the order size. Orders only after the risk gate
passes; every submission logged to the decisions table for 1.1's reflection loop.

**Effort:** M. **Prerequisite:** 1.2 must be solid first — never forward an
unsized, un-gated decision to a broker.

---

## Suggested order

| # | Item | Why this position |
|---|------|-------------------|
| 1 | 1.2 Risk layer + sizing | Deterministic, no cost, immediate safety; prerequisite for 4.1 |
| 2 | 1.1 Decision memory | Highest research-backed ROI; trade history just shipped feeds it |
| 3 | 2.1 Rebuttal round | Small, visible quality win; cheap A/B via config |
| 4 | 2.3 Reliability + per-agent models | Cuts cost (cheap analysts) before the expensive backtests |
| 5 | 2.2 Backtest harness | The measurement tool for tuning everything above |
| 6 | 3.1–3.3 | Breadth, once measurement exists |
| 7 | 4.1 Alpaca | Only after risk + memory are proven |

## Bibliography

- TradingAgents framework — [arXiv 2412.20138](https://arxiv.org/html/2412.20138v5) · [repo (decision log, risk team)](https://github.com/TauricResearch/TradingAgents)
- Multi-agent debate rounds — Du et al., [arXiv 2305.14325](https://www.alphaxiv.org/abs/2305.14325) · [rounds-vs-cost](https://aclanthology.org/2026.acl-srw.1.pdf)
- Layered memory — FinMem [arXiv 2311.13743](https://arxiv.org/abs/2311.13743) · TradingGPT [arXiv 2309.03736](https://www.alphaxiv.org/abs/2309.03736) · [overview table](https://www.emergentmind.com/topics/multi-agent-llm-financial-trading)
- Position sizing — [vol-parity & Kelly basics](https://quanterlab.com/articles/foundations-position-sizing) · [vol targeting intro](https://quantpedia.com/an-introduction-to-volatility-targeting) · [Kelly f=μ/σ², MacLean et al.](https://breakingalpha.io/insights/position-sizing-algorithmic-trading)
- Backtesting rigor — [walk-forward framework, arXiv 2512.12924](https://arxiv.org/html/2512.12924v1) · [LLM look-ahead/memorization bias](https://paperswithbacktest.com/course/look-ahead-bias-llm-trading)
- Sentiment — [StockTwits + FinBERT, PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC10280432)
- CrewAI in production — [lessons 2026](https://www.agilesoftlabs.com/blog/2026/06/crewai-in-production-2026-real-lessons)
- LLM trading evaluation metrics — [end-to-end LLM trading system, arXiv 2502.01574](https://arxiv.org/html/2502.01574v1)
