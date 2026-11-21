# Options replay protocol v1

Use **Backtests** in the existing BetterTradingAgents dashboard. The historical lane requires an entitled local bid/ask import. Select **Synthetic mechanics test** to exercise the same running, history, accounting, and export paths on invented prices and signals. **Stock** runs the existing mock stock workflow in the source installation and opens existing stock reports.

## Presets and signals

| Mode | Preset | Entry DTE | Target DTE | Exit session |
|---|---|---:|---:|---:|
| `options_intraday_v1` | `intraday_7_14_dte` | 7–14 | 10 | Entry session |
| `options_swing_v1` | `swing_5_sessions` | 14–21 | 18 | 5, counting entry as 1 |
| `options_swing_v1` | `swing_10_sessions` | 28–35 | 32 | 10, counting entry as 1 |

The study universe is NVDA, AMD, META, MSFT, AAPL, and TSLA. Only a BUY can open one whole long call. SELL and HOLD leave the money in cash. There is no 0DTE, exercise, shorting, put conversion, spread, or discretionary exit. The 23 archived dates contain 138 Fast calls. The stored manifest records `deepseek-ai/DeepSeek-V4.1-Flash`, archived policy `2026-10-e`, short-term inputs, the dirty source revision, and possible historical model memorization. Actual cutoff and generation times were unavailable. For scheduling, the archive uses 23:59 America/Chicago on each saved date. The 138 comparator signals use the existing `momentum_decision` on the stock reconstruction’s dated closes, matching the archived aggregate of 73 BUYs. Exact original momentum rows remain unverified. Stock-sizing multipliers play no role in this study.

The intraday historical lane is an **Archived daily-signal intraday options replay.** Its Fast calls come from daily inputs and a five-trading-day forecast. The test measures same-day liquidation of those signals. `SignalArchive` accepts a separate timestamped candidate with model, prompts and prompt hash, settings, input source, input cutoff, generation and availability timestamps, and costs. The dashboard uses the frozen archive in v1. Incremental archived replay inference costs are zero, and historical sunk costs are unavailable.

## Selection, execution and capital

The XNYS regular-session calendar supplies holidays and shortened sessions for the six standard equity options. Select at 08:40 Chicago time at the first eligible session following a saved signal. Exit 15 minutes before scheduled close. Require the exit date to precede actual expiration and the exit timestamp to precede the verified last trading time. Keep the chosen contract through exit. Eligible calls must be known listed at selection time, have verified 100-share deliverables and a valid two-sided quote, trade within 2% of spot in strike distance, and show at most 10% relative and $0.20 absolute spread. Rank by distance from target DTE, strike distance, relative spread, expiration, strike, and contract ID.

Initialize two independent $2,500 cash accounts per run. Allocate at most 15% of each arm’s equity after research charges to the entire batch, including entry and exit fee reserves. A request can lower the research cap. Rank simultaneous picks by relative spread, percentage strike distance, then ticker. Use whole contracts and recheck the shared budget against the actual modeled request-time fill. Reject unaffordable candidates and continue down the list. Carry balances forward. Swing cohorts do not overlap, and an arm with an open delayed or unresolved position cannot initiate another cohort. Unpaid research costs make the run incomplete.

Default charges are $0.65 commission per contract per side, $0 other fees, and $0.01 adverse slippage per quoted unit per side, stressed at $0.02. These are configurable research assumptions. A $0.01 premium difference on a verified 100-share option is $1. Round cash flows to cents and reserve both sides’ fees. The primary account includes research costs. Trading-only figures add their dollars back on the same execution path. They do not rerun the account at a different risk budget.

- **Tick quotes.** Select using the most recent complete, valid quote observed in the last two seconds and available by selection. Require at least one displayed contract on the applicable side for each fill. Model order submission one second after selection and allocate against the eligible quotes visible at a common fill time three seconds after selection. The observation must occur on or after order submission. Buy ask plus slippage and sell bid minus slippage. The underlying observation must be available at selection and at most two minutes old.
- **Minute quote bars.** Use the closing bid and ask of the most recent completed, available bar for selection. Use the same one-second order delay and a common fill time three minutes after selection. Use a different bar whose start is no earlier than the order and whose observation and availability are within the relevant session. This is an execution estimate. Use observed size if supplied, reject explicitly zero size, and report unknown size. Preserve the source’s actual availability timestamps, including publication delays where applicable.
- **Exit exceptions.** A fill more than two minutes after the scheduled target is delayed. Seek the first supported, executable regular-session quote for the same contract before last trade or an effective adjustment. A later-date intraday exit counts as a same-day breach. Unresolved positions retain their cost basis and exit reserve, block reuse, and withhold final equity for that arm. A verified adjustment that affects an open position requires settlement evidence, so v1 leaves the position unresolved. Expiration outcome stays unresolved when documented settlement evidence is absent.

## Execution enforcement and retries

The original replay already used independent simulated ledgers and one shared selector and execution path for Fast and momentum. It enforced one whole long call on BUY, the six-stock universe, the preset DTE range, candidate ranking, verified deliverable, entry spreads, causal modeled fills, fee reserves, and scheduled exits. Closing an owned call uses the existing contract, quote and time constraints. Entry DTE, strike distance, spreads, and premium budget are selection and opening controls.

Policy version `options-replay-v1-enforcement-1` closes the request and replay gaps. The API and replay enforce the 15% ceiling and replay checks the frozen preset and scheduled cohort timestamps before processing any fills. Quote evidence is restricted to regular-session observations inside the contract’s verified trading lifetime. An older late-arriving observation cannot replace a newer observed quote for a sale. The dataset validator rejects conflicting spot prices with identical ordering evidence. The ledger applies each batch once per arm and removes ownership after a sale. Differences in research charges and available equity can still produce different outcomes for the two arms.

Options run requests require a UUID `Idempotency-Key` on `POST /api/backtests/runs`. The dashboard generates it and reuses it with the same payload after an uncertain response. A repeated key and request return the original run, including after server restart. Using that key with different inputs is rejected. A server stop before completion leaves an interrupted run with its receipt. A deliberately new run uses a new key and separate simulated accounts. The options routes have no broker execution or account-mode input.

The manifest records `account_mode: "simulated"` and `execution_policy_version`. Decisions link BUY intents to the chosen contract, spot and selection quote, modeled fill target and quote, batch commitment, and acceptance or skip reason when applicable. When the quote check rejects an observed candidate, the decision retains that quote and the evidence rejection code. Trade records retain the linked intent, cash changes, fees and exit reserve, execution timestamps and quotes, and unresolved status if supported closing evidence is absent.

`uv run test options_backtest options_retry` checks rejection of live and over-cap requests, broker isolation, shared committed cash after research and modeled fees, stale or invalid evidence, closing an owned call below the original entry DTE band with a wider spread, Fast and momentum parity, and duplicate entry and settlement calls. It also exercises concurrent identical POSTs, restart recovery, and the behavior after an interrupted claim.

## Coverage and reporting

Readiness evaluates date coverage before any performance calculation. Actual contract picks only inspect observations available at selection. An observation can still be missing or invalid inside a covered window. Keep that signal, reason, and costs. Each execution scenario reports execution status, market-data status, and scheduled, usable, and missing paired observations with reasons. The scheduled comparison mark is two minutes after the exit target. A missing mark remains unscored. The next pair also needs a valid preceding scheduled mark, so reported returns never silently span a gap. End balances are separately timestamped at the data cutoff and can reflect late exits. They do not substitute for missing scheduled marks. Results carry all unsupported dates and an **Incomplete** label when execution, required data, or comparison is incomplete. Cohort-end drawdown uses cohort-end marks only. Fewer than three executions is labeled insufficiently executable for this initial display. Results are exploratory and do not grant trading authority or use the stock study’s economic hurdle.

## Local data import

See `data-feasibility.md` for current provider findings. Export a targeted, locally licensed dataset as a normalized JSON package per preset, then run:

```powershell
uv run --no-sync python -m app.backtest.options schema
uv run --no-sync python -m app.backtest.options import intraday_7_14_dte C:\path\to\normalized-quotes.json
uv run --no-sync python -m app.backtest.options plan intraday_7_14_dte
```

The package schema requires `schema_version: 1`, matching `preset`, `source: "historical"`, `resolution: "tick"` or `"minute"`, provider and entitlement notes, a timezone-aware `covered_through`, and the following records:

| Record | Required information |
|---|---|
| `coverage[signal_date][ticker]` | Evidence flags `discovery`, `entry_quotes`, `exit_quotes`, `underlying`, and `adjustments`, with `note` when a range is unsupported. Flags apply to the intended full selection universe and the planned entry and exit windows, including when no contract meets the frozen filters. |
| `contracts[]` | Stable ID, underlying ticker, right, strike, actual expiry, evidenced listing/availability timestamp, last-trade timestamp, multiplier, and deliverable. Use `standard_100_shares` only when verified. |
| `underlying[]` | Ticker, unadjusted contemporaneous price, observed and available timestamps. For bars, use at or after the completed bar’s end. |
| `quotes[]` | Contract ID, bid, ask, optional bid/ask sizes, observed and available timestamps, and `bar_start` for minute quotes. `observed_at` for a minute quote is its completed end time. Round finer-than-microsecond source times upward. Set `source_timestamp` to the original RFC 3339 event time, with up to nine fractional digits, and `source_sequence` to the provider’s nonnegative sequence when available. The importer rejects conflicting updates with identical ordering evidence. Selection and execution use the latest visible quote when updates share a normalized availability timestamp. |
| `adjustments[]` | Known deliverable changes, contract identifier, effective timestamp, and evidence. |

Start with the three feasibility dates. Pack full as-of discovery results and targeted market-data windows. Use entry quotes for every eligible candidate and sufficient exit coverage for any contract the frozen selector could select. A scalable acquisition process can select with the entry tape, then request only those contracts’ exit quotes and adjustments. Keep coverage flags and the logged list of unsupported dates independent of returns. Packages must be at most 128 MiB. Market data and generated results live locally under `local-ops/backtests/options/`; omit API keys and preserve license restrictions.

The app reads `local-ops/backtests/options/<mode>/<preset>/data/historical.json`. Runs persist under `<mode>/<preset>/<config_hash>/<run_id>/` with `manifest.json`, `status.json`, and `result.json`. The config hash covers the request and rules, signal and data fingerprints, calendar and execution assumptions, and the executing options engine identity. The source server records its Git commit and the options package SHA-256 at startup, with a dirty-source fingerprint when that package has uncommitted changes. A bundled server records the executing build hash. Archived signal provenance remains separate. Legacy runs stay readable and may lack engine and comparison metadata. Results include contract receipts, timestamps, entry outcomes, paired marks, primary and stressed ledgers, and the saved manifest. The separate stock cache and archive remain available. Cancelled or interrupted runs keep their own status and do not masquerade as results.
