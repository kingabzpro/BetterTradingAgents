# BetterTradingAgents development roadmap

Practical next steps for turning the current research demo into a trustworthy
decision workspace. Last updated: 2026-09-05.

This roadmap is ordered by user value and risk reduction, not novelty. The app
already has enough agents. The next releases should make existing analysis easier
to judge, easier to control, and easier to evaluate before adding execution.

## Product rules

- Evidence before persuasion. Show freshness, missing inputs, disagreement, and
  risk adjustments beside every call.
- Keep confidence honest. It means evidence strength until historical results
  demonstrate calibration; it is not a probability of profit.
- Deterministic code owns money and limits. LLMs summarize evidence; `app/risk.py`
  sizes and gates positions.
- Paper first, live never by accident. A recommendation and an order are separate
  user actions with a review step between them.
- Keep the current stack. FastAPI, SQLite, vanilla HTML/CSS/JS, and the installed
  dependencies cover the planned work.

## Current baseline

Already shipped:

- parallel technical, fundamental, news, sentiment, and forecast research
- bull/bear debate with an optional rebuttal round
- portfolio-aware deterministic sizing and exposure caps
- decision memory graded against realized returns and SPY
- walk-forward backtests, with current-vintage fundamentals clearly flagged
- per-role models, classified retries, and optional token streaming
- durable run history, reconnect recovery, per-ticker retry, and a one-hour
  analysis cache
- source links, data timestamps, provider labels, responsive result cards, and
  keyboard-visible focus styles

## Priority map

| Priority | Outcome | Area | Effort | Gate |
|---|---|---|---|---|
| P0.1 | Decision brief users can audit in seconds | UI/UX + API | M | Next release |
| P0.2 | Cancel, rerun, and recover without guessing | UX + run logic | S | Next release |
| P0.3 | Accessible, mobile-safe core journey | UI quality | S-M | Next release |
| P1.1 | Historical confidence calibration | Logic + evaluation | M | Before execution |
| P1.2 | Honest experiment and backtest workflow | Evaluation | M | Before tuning prompts or depth |
| P1.3 | Portfolio-level concentration risk | Risk logic + UI | M | Before execution |
| P1.4 | Watchlist and decision-change workflow | Product UX | M | After P0 |
| P2.1 | Alpaca paper-order workflow | Integration | L | After P1.1-P1.3 |
| P2.2 | Deployment and operational hardening | Platform | M | Before any shared deployment |

## P0 - Make today's analysis useful

### P0.1 Decision brief and trust state

**Problem.** The summary table is compact, but users must open each result to learn
when the data was fetched, which inputs were missing, why analysts disagreed, and
whether the risk gate changed the manager's call. The model's original decision is
currently overwritten by the risk gate, so that change cannot be shown explicitly.

**Build.**

- Preserve `manager_decision` and `manager_confidence` on `StockAnalysis`, then keep
  `decision` and `confidence` as the final risk-adjusted values.
- Add a server-computed `data_quality` object: data timestamp, expected/available
  analysts, failed analysts, stale flag, and provider fallbacks. Define stale from
  the selected outlook, not one global threshold.
- Change the first visible result row to show: final call, evidence-strength label,
  current price, horizon, data age, analyst coverage, and a risk-adjusted badge.
- Put a compact signal split directly below it, such as `3 bullish / 1 neutral /
  1 bearish`. Derive it from existing analyst results; do not add an LLM call.
- Make the expanded brief follow one reading order: decision summary, risk changes,
  bull/bear disagreement, analyst evidence, sources, then prior outcomes.
- Add two structured manager fields, `would_upgrade_if` and `would_downgrade_if`,
  limited to conditions supported by the dossier. Label them as conditions, not
  alerts or guaranteed price levels.
- On cached results, show `cached` and the original `as_of` time. Never make a
  zero-second cache hit look like newly collected research.

**Acceptance.**

- A user can identify freshness, coverage, disagreement, and risk intervention
  without expanding a card.
- A manager BUY downgraded to HOLD renders `Manager: BUY -> Final: HOLD` with the
  exact deterministic risk flag.
- Old persisted runs still parse with safe defaults for every new field.
- The table remains readable at 360 px, 768 px, and 1440 px without horizontal page
  scrolling.

**Why now.** FINRA identifies inaccurate or misleading GenAI output presented as
fact as a decision-making risk. The UI should therefore make data provenance and
system intervention part of the primary result, not secondary detail
([FINRA 2026 GenAI guidance](https://www.finra.org/rules-guidance/guidance/reports/2026-finra-annual-regulatory-oversight-report/gen-ai)).

### P0.2 Run controls: cancel, rerun, and clear status

**Problem.** Runs survive navigation and individual failures can be retried, but a
user cannot cancel a slow or mistaken run. The backend also has no `cancelled`
state, and the history screen does not offer a one-click rerun with the same
outlook and depth.

**Build.**

- Retain the task created by `RunStore.create()` and add
  `POST /api/runs/{run_id}/cancel`.
- Add `cancelled` to run status models. Cancel child ticker tasks, persist the
  partial results, and always emit one terminal `analysis_completed` event.
- Add a visible Cancel button during a run. After completion, replace it with
  `Run again` and `Analyze another`; keep the original tickers, outlook, and depth.
- In Runs, add `Rerun` for completed/failed/cancelled items and explain partial
  results in plain text.
- Keep completed ticker results when the rest of a run is cancelled. Never record
  a decision for an interrupted ticker.
- Explain that cancellation stops this pipeline from advancing but may not revoke
  an LLM request already accepted by an external provider.

**Acceptance.**

- Cancel reaches a terminal UI state within one second after the server accepts it.
- Refreshing a cancelled run restores the same partial results and status.
- Repeated cancel requests are harmless, and a cancelled run cannot later flip to
  completed.
- One offline check covers cancellation during data fetch and during agent work.

### P0.3 Accessibility and small-screen completion pass

**Build.**

- Test the full keyboard path: add/remove ticker, select outlook/depth, start,
  cancel, open evidence, retry, add a demo position, and close it.
- Announce meaningful state changes through the existing live regions, but do not
  announce every token or progress tick.
- Verify focus returns to the initiating control when disclosures or review panels
  close and moves to the results heading when a run finishes.
- Give every icon-only control a programmatic name and a minimum 44 by 44 CSS-pixel
  target where layout allows.
- Ensure tables reflow into labeled cards at narrow widths and that 200% text zoom
  does not hide actions or totals.
- Add one automated browser smoke test for the core journey plus a documented
  manual screen-reader check. Do not change frontend frameworks for this work.

**Acceptance.** The home, results, runs, and portfolio journeys work without a
mouse; focus is always visible; status changes are announced once; no core action
is lost at 320 CSS pixels or 200% zoom.

W3C's guidance specifically calls for visible keyboard focus and programmatic
status messages, including progress and completion updates
([focus visible](https://www.w3.org/WAI/WCAG22/Understanding/focus-visible.html),
[status messages](https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html)).

## P1 - Prove and improve the logic

### P1.1 Calibrate confidence from outcomes

**Problem.** Manager confidence is model-authored evidence strength. A value such as
`0.72` has not been shown to mean a 72% win rate, yet numeric percentages can imply
that precision.

**Build.**

- Keep the UI label `evidence strength`; use Low/Moderate/Strong as the primary
  display and the raw value as secondary detail.
- Add an evaluation query over mature decisions grouped by decision, outlook,
  depth, model, and confidence bucket. For BUY/SELL, return directional hit rate,
  positive-alpha rate, mean alpha, and median alpha. For HOLD, show missed upside
  and avoided downside instead of inventing a win rate. Always return sample size.
- Show `Track record unavailable` until a bucket has enough mature observations.
  Use a configurable minimum, default 30, and always show `n`.
- Add a manager probability field only after its success event and horizon are
  frozen in the schema. Score it with a reliability table and Brier loss; do not
  relabel existing confidence.
- Version decision policy and prompt configuration in every recorded decision so
  outcomes from materially different systems are not pooled silently.

**Acceptance.** No screen calls model confidence a probability. Historical rates
always show their sample size and configuration scope. A calibration report can be
regenerated from SQLite with one command and no LLM calls.

Reliability diagrams compare predicted probabilities with observed frequencies,
while proper scores such as Brier loss assess probabilistic predictions
([scikit-learn calibration guide](https://scikit-learn.org/stable/modules/calibration.html)).

### P1.2 Honest experiment and backtest workflow

**Problem.** The harness can replay decisions, but current-vintage fundamentals
leak later information into historical runs. It also lacks a fixed comparison
matrix, an untouched test period, and uncertainty around reported metrics. Tuning
debate depth on the same dates used for the headline result would overfit the demo.

**Build.**

- Make the honest default exclude the fundamental analyst from historical replay
  when point-in-time fundamentals are unavailable. Keep current-vintage data only
  behind `--allow-current-fundamentals`, with a large report warning.
- Persist a manifest beside each report: code revision, model/provider, prompt
  version, ticker universe, dates, costs, data snapshot hashes, and random seeds.
- Add simple baselines: all HOLD, buy-and-hold, and the existing deterministic
  discovery momentum score. A multi-agent result must beat a cheap baseline to
  justify its extra cost.
- Add paired experiment mode for exactly one change at a time: depth, rebuttals,
  forecast, sentiment, or model. Reuse the same cached snapshots.
- Split date ranges into tune and untouched test periods. Report sample size and a
  bootstrap interval for mean alpha; avoid promoting a winner when intervals are
  too wide or fewer than 30 positioned decisions exist.
- Move debate-depth tuning here. Do not set `DEBATE_ROUNDS=3` merely because one
  backtest has a higher Sharpe ratio.

**Acceptance.** The same manifest and cache reproduce the same mock report. Default
reports contain no current-vintage fundamentals. Every comparison states the
baseline, holdout dates, sample size, costs, and uncertainty.

### P1.3 Portfolio-level concentration risk

**Problem.** Current risk rules cap each ticker, total invested capital, and cash,
but five highly correlated technology positions can pass those limits while still
behaving like one concentrated bet.

**Build.**

- Extend the deterministic risk snapshot with sector concentration, largest-name
  concentration, and 60-day return correlation among priced holdings.
- Before a demo buy, calculate and show portfolio exposure before and after the
  proposed position.
- Add two explainable brakes: a configurable sector cap and a correlated-exposure
  warning. Start warnings-only, measure frequency, then enable blocking only if
  tests show sensible behavior.
- Treat missing prices or insufficient history as `unknown`, never zero risk.
- Keep the calculation in `app/risk.py`; no risk-agent LLM and no new numerical
  dependency are needed for the first version.

**Acceptance.** Adding a stock highly correlated with the largest holding produces
an explicit warning and before/after exposure. Missing market data cannot lower the
reported risk. Existing ticker and cash caps remain intact.

Diversification can reduce overall portfolio risk, including concentration within
an asset class ([Investor.gov diversification guide](https://www.investor.gov/additional-resources/general-resources/publications-research/info-sheets/beginners-guide-asset)).

### P1.4 Watchlist and change detection

**Build.**

- Let users save a result or discovery candidate to a SQLite-backed watchlist with
  an optional note and preferred outlook/depth.
- Show last call, current call, decision change, evidence-strength change, price
  move since analysis, data age, and unresolved risk flags.
- Provide `Analyze selected` and `Analyze all`, still subject to `MAX_TICKERS`.
- Start with manual refresh. Add scheduled analysis, email, or push notifications
  only after users demonstrate that manual watchlists are useful.

**Acceptance.** A saved ticker survives restart, links to its last comparable run,
and distinguishes `no change` from `not reanalyzed`.

## P2 - Connect safely and operate reliably

### P2.1 Alpaca paper trading

**Scope.** Paper accounts only. Real-money endpoints, automated submission, options,
short selling, and background strategy execution are explicitly out of scope.

**Build.**

- Use the installed `httpx` dependency for a small `alpaca-paper` adapter; do not
  add an SDK until the REST surface becomes painful.
- Keep Analyze and Place paper order as separate actions. The order action opens a
  review step showing symbol, side, quantity/notional, order type, time in force,
  reference-price timestamp, buying power, and portfolio exposure before/after.
- Require an explicit confirmation. Recommendations never auto-submit.
- Generate and persist a unique `client_order_id` before submission. Retries reuse
  it, so a network timeout cannot create a duplicate order.
- Store the full lifecycle: accepted, partial fill, filled, cancelled, expired,
  rejected, and replaced. Reconcile by REST polling on submission and page load;
  add a streaming client only if real usage needs lower latency.
- Add cancel for open orders, a connection test, a paper-mode banner, and a local
  kill switch that disables all submissions.
- Keep Alpaca credentials server-side and redact them from logs and errors.

**Acceptance.** A duplicate HTTP retry cannot place a second order. Partial fills
update quantity and average price correctly. A rejected order never becomes a
portfolio position. Tests use a fake HTTP transport and make no broker calls.

Alpaca documents partial fills and several simulation limits, including simplified
market impact and liquidity behavior, so the UI must not present paper results as
live-trading proof ([paper trading limitations](https://docs.alpaca.markets/us/docs/paper-trading)).
Alpaca also supports client-assigned order IDs and multiple terminal/non-terminal
order states ([working with orders](https://docs.alpaca.markets/us/docs/working-with-orders)).

### P2.2 Deployment and operational hardening

**Problem.** The current client ID is a browser-generated history filter, not
authentication. Run-detail and portfolio endpoints are appropriate for a local
single-user app but not for a public multi-user deployment.

**Build.**

- State `single-user local app` clearly in configuration and startup logs.
- Before shared deployment, add real authentication and owner checks to run,
  watchlist, portfolio, decision-memory, and order endpoints.
- Add CSRF protection for cookie-authenticated writes, request-size limits, and
  per-user analysis concurrency limits.
- Replace import-time SQLite schema edits with numbered, transactional migrations
  and a documented backup/restore command.
- Emit structured run metrics: duration by stage, failure class, provider fallback,
  cache hit, token use, and estimated model cost. Never log prompts containing
  credentials or imported portfolio rows.
- Add readiness checks for the database and configured providers; keep `/api/health`
  free of secrets.

**Acceptance.** A user cannot read or mutate another user's data. A failed migration
rolls back. Operators can distinguish provider, model, data, and validation failures
without exposing sensitive payloads.

## Explicitly deferred

- More analyst personas: measure the five current researchers first.
- More than two debate rounds: only ship if the paired holdout experiment wins.
- Live chain-of-thought UI: the current optional token stream is mostly final JSON
  and should remain off by default.
- Real-money trading or unattended orders: outside this educational project's
  safety boundary.
- Gamified streaks, confetti, leaderboards, and pressure notifications: they do not
  improve decision quality and can encourage activity for its own sake. The SEC
  notes that engagement-oriented trading features can create conflicts between
  platform goals and investor interests
  ([SEC digital engagement statement](https://www.sec.gov/newsroom/speeches-statements/gensler-dep-request-comment)).
- A frontend framework migration: revisit only when vanilla code blocks a measured
  product need.

## Release gates

| Release | Must be true |
|---|---|
| Trustworthy research | P0 complete; freshness, coverage, cancellation, keyboard, and mobile checks pass |
| Evidence-based logic | P1.1 and P1.2 complete; a holdout report with baselines and sample sizes exists |
| Paper execution | P1.3 and P2.1 complete; duplicate, partial-fill, rejection, and cancel tests pass |
| Shared deployment | P2.2 complete; owner isolation and migration rollback are tested |

## Research references

- [FINRA 2026 GenAI guidance](https://www.finra.org/rules-guidance/guidance/reports/2026-finra-annual-regulatory-oversight-report/gen-ai)
- [SEC statement on digital engagement practices](https://www.sec.gov/newsroom/speeches-statements/gensler-dep-request-comment)
- [W3C WCAG 2.2 quick reference](https://www.w3.org/WAI/WCAG22/quickref)
- [scikit-learn probability calibration guide](https://scikit-learn.org/stable/modules/calibration.html)
- [Investor.gov asset allocation and diversification guide](https://www.investor.gov/additional-resources/general-resources/publications-research/info-sheets/beginners-guide-asset)
- [Alpaca paper trading documentation](https://docs.alpaca.markets/us/docs/paper-trading)
- [Alpaca order lifecycle documentation](https://docs.alpaca.markets/us/docs/working-with-orders)
- [TradingAgents paper](https://arxiv.org/html/2412.20138v5)
