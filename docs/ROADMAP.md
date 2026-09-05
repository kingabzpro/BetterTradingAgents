# BetterTradingAgents — Development Roadmap

Research-backed implementation plans for everything after the current quick wins.
Each item lists the evidence behind it, the concrete design in this codebase, and
acceptance criteria. Last updated: 2026-09-05.

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
- **1.1 Decision memory**: `app/memory.py` + a `decisions` table in `portfolio.db`
  — every completed run records its call; past decisions are graded against
  realized closes (own return + alpha vs SPY over `MEMORY_HORIZON_DAYS`, default
  21; mature outcomes computed once and stored, younger ones re-graded cheaply
  as partial windows); the manager dossier gets the 3 most recent same-ticker
  reflections + 2 cross-ticker lessons, the result card shows the track record;
  reflections are deterministic sentences unless `MEMORY_REFLECT_WITH_LLM=1`
- **2.3 LLM reliability + per-role models**: per-role LLM overrides
  (`LLM_MODEL_MANAGER`, `LLM_MODEL_ANALYSTS`, `LLM_MODEL_DEBATE` + matching
  `_BASE_URL`/`_API_KEY`, falling back to the global `LLM_*`) — cheap fast
  researchers, a stronger model for the final call; classified retries
  (429/5xx → backoff 1s/4s, malformed JSON → one retry with the error fed
  back into the task, `response_format` rejection → JSON mode dropped for the
  role and the call retried); `response_format={"type": "json_object"}` on by
  default via `additional_params`; token usage from crew outputs summed per
  run and shown in `agent_completed` events, `StockAnalysis.token_usage` and
  the result card
- **2.2 Backtest harness**: `app/backtest/` with a walk-forward CLI
  (`uv run python -m app.backtest --tickers NVDA,AMD --start ... --end ... --step 21`)
  — point-in-time snapshots (6mo OHLCV ending at T, Finnhub news filtered to
  `published <= T`, current-vintage fundamentals stated as a known bias) fed
  to the real pipeline via `market_data` injection with `live_context=False`
  (no portfolio/memory look-ahead, no decision recording); grading in pure
  functions (BUY long / SELL short-or-0 / HOLD 0, minus 2×5bp round-trip
  cost, alpha vs SPY over the horizon); aggregates (hit rate, cumulative,
  Sharpe, max drawdown, buy-and-hold baseline); SQLite snapshot cache keyed
  ticker+date so warm re-runs make zero network calls (`BACKTEST_OFFLINE=1`
  enforces it); mock mode default, `--llm` gated behind a printed cost
  estimate; JSON+markdown reports in `docs/backtests/` with the
  memorization-risk flag; `scripts/backtest_smoke.py` regenerates the
  3-ticker × 6-date baseline

- **3.1 Sentiment / social analyst**: `app/agents/sentiment.py` — a 5th researcher
  in the stage-1 gather (Medium/Expert depth) reading Reddit/StockTwits posts from
  an Olostep site-restricted search (`{ticker} stock (site:reddit.com OR
  site:stocktwits.com)`, `MarketData.social`), same `AnalystResult` schema; fewer
  than 3 posts reads as neutral with low confidence ("social volume too thin to
  mean anything") in both LLM and mock modes; social threads surface as
  `kind="social"` source references; the risk gate's missing-input brake now counts
  4 analyst slots; backtest replays get an empty social set (no point-in-time
  archive) so the honest neutral applies; UI: 5th research row + evidence card,
  old runs show "Not recorded"
- **3.2 Live reasoning stream**: LLMs can be built with `stream=True`
  (`STREAM_REASONING`) so CrewAI emits `LLMStreamChunkEvent`s; a scoped
  `add_stream_sink` per agent run (concurrent agents never see each other's
  tokens) forwards content chunks as `agent_token` SSE events — live-only
  (never persisted for reconnect replay), sliding ~2 KB window in the UI pane,
  16 KB per-agent forward guard, thinking deltas excluded; provider stream
  rejection is classified, drops streaming for the role and retries (JSON
  mode preserved), exactly like the `response_format` fallback; mock mode
  streams the deterministic summary word-by-word, skipped in backtest
  replays; UI: collapsible per-agent reasoning pane behind the agent row.
  **Shipped off by default after live evaluation**: what streams is mostly
  the final JSON blob, which reads as noise next to the result card — enable
  with `STREAM_REASONING=1` if wanted

---

## Phase 1 — Learn from outcomes, size the risk

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

Shipped — see the baseline above.

### 2.3 LLM reliability & per-agent models

Shipped — see the baseline above.

---

## Phase 3 — Breadth & experience

### 3.1 Sentiment / social analyst (4th researcher)

Shipped — see the baseline above.

### 3.2 Stream agent reasoning live (README roadmap)

Shipped — see the baseline above.

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

Phases 1 and 2 are complete (see the baseline above), and 3.1 Sentiment analyst
and 3.2 Live reasoning stream are shipped. Remaining work, in order:

| # | Item | Why this position |
|---|------|-------------------|
| 1 | 3.3 Debate-depth tuning | Needs the 2.2 harness, which exists — run the `DEBATE_ROUNDS` 1 vs 2 vs 3 grid and set the default from data |
| 2 | 4.1 Alpaca | Only after risk + memory are proven |

## Bibliography

- TradingAgents framework — [arXiv 2412.20138](https://arxiv.org/html/2412.20138v5) · [repo (decision log, risk team)](https://github.com/TauricResearch/TradingAgents)
- Multi-agent debate rounds — Du et al., [arXiv 2305.14325](https://www.alphaxiv.org/abs/2305.14325) · [rounds-vs-cost](https://aclanthology.org/2026.acl-srw.1.pdf)
- Layered memory — FinMem [arXiv 2311.13743](https://arxiv.org/abs/2311.13743) · TradingGPT [arXiv 2309.03736](https://www.alphaxiv.org/abs/2309.03736) · [overview table](https://www.emergentmind.com/topics/multi-agent-llm-financial-trading)
- Position sizing — [vol-parity & Kelly basics](https://quanterlab.com/articles/foundations-position-sizing) · [vol targeting intro](https://quantpedia.com/an-introduction-to-volatility-targeting) · [Kelly f=μ/σ², MacLean et al.](https://breakingalpha.io/insights/position-sizing-algorithmic-trading)
- Backtesting rigor — [walk-forward framework, arXiv 2512.12924](https://arxiv.org/html/2512.12924v1) · [LLM look-ahead/memorization bias](https://paperswithbacktest.com/course/look-ahead-bias-llm-trading)
- Sentiment — [StockTwits + FinBERT, PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC10280432)
- CrewAI in production — [lessons 2026](https://www.agilesoftlabs.com/blog/2026/06/crewai-in-production-2026-real-lessons)
- LLM trading evaluation metrics — [end-to-end LLM trading system, arXiv 2502.01574](https://arxiv.org/html/2502.01574v1)
