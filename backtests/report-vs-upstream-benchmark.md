# Benchmark: BetterTradingAgents vs upstream TradingAgents

Generated 2026-10-28. BetterTradingAgents `65a286d` (v0.4.0) vs TauricResearch/TradingAgents `c454491` (v0.7.1, pushed 2026-10-27).

## What was run

- Grid: NVDA, AMD, MSFT at 2026-09-03 and 2026-09-25 (6 decisions per framework; both dates' 21-trading-day grade windows have matured).
- Same model for both: `deepseek-ai/DeepSeek-V4.1-Flash` through the same OpenAI-compatible endpoint, temperature 0.0.
- BTA: `python -m app.backtest --horizon 21 --llm` at all three depths - fast (3 agents), pro (7), max (10 with debates) - same point-in-time data rules, fundamentals excluded, news filtered to the decision date.
- Upstream: `TradingAgentsGraph.propagate()` per cell with upstream defaults (4 analyst teams with tool loops, bull/bear debate, trader, 3-way risk debate, research + portfolio managers; point-in-time SEC as-filed fundamentals, window-filtered news; macro/FRED skipped for lack of a key; Reddit sentiment largely rate-limited during the run).
- Both sides graded by the identical rule (BTA's `app/memory.py`: BUY right when alpha vs SPY >= +1%, SELL right when alpha <= -1%, HOLD right when the stock moved <= 2%; entry = last close on/before the date, exit = first close 21 trading days later).

## How the benchmark works

A benchmark is a grid of **cells**: one ticker at one historical date. Each cell is one trade decision, made with only the information that existed on that day (prices up to the date, news published on or before it, fundamentals as filed), so nothing from the future leaks into the call. Both frameworks run their full agent pipeline once per cell at temperature 0 and return a single rating - BUY / HOLD / SELL here, upstream's five-way Buy...Sell mapped onto the same three directions.

Every call is then held for a fixed window (21 trading days here) and graded the same way, using BTA's own grading rule for all frameworks:

- The **return** is the stock's move from the last close on or before the decision date to the first close after the window closes.
- **Alpha** is that return minus SPY's move over the same window, because a BUY that makes +3% while the market makes +5% actually destroyed relative value.
- The verdict: a BUY is *right* when alpha is +1% or better, a SELL when alpha is -1% or worse, and a HOLD is *right* whenever the stock moved 2% or less (or, as here, when the slide it sidestepped was big).
- The money view assumes $10,000 on every BUY, long-only; SELL and HOLD sit in cash and score $0.

Six cells is an anecdote, not a verdict. The point of the design is that cells are independent and cheap to multiply: the same commands with a wider grid give a real sample.

## How to add more trades and more stocks

The grid is two knobs: `--tickers` (any comma list) and the date range with `--step` (days between decisions). Cells = tickers x dates, and each cell is one graded trade. Rough planning numbers from this run, per cell per framework:

| Framework | Cost per cell | Time per cell | 40 cells (5 tickers x 8 weekly dates) | 130 cells (10 tickers x 13 weekly dates) |
|---|---|---|---|---|
| BTA fast | ~$0.005 | ~9-30 s | ~$0.20, minutes | ~$0.65, ~15-30 min |
| BTA pro | ~$0.011 | ~9-30 s | ~$0.45 | ~$1.40 |
| BTA max | ~$0.015 | ~9-30 s | ~$0.60 | ~$2 |
| Upstream | ~$0.07-0.12 (measured) | ~3-7 min | ~$3-5, 2-4 h | ~$9-16, 6-15 h |

Recipes:

```
# a weekly-resolution study, 10 tickers x 13 dates = 130 decisions per framework
uv run python -m app.backtest --tickers NVDA,AMD,MSFT,AAPL,GOOGL,META,TSLA,AMZN,JPM,XOM \
  --start 2026-07-01 --end 2026-09-25 --step 7 --horizon 21 --depth pro --llm --yes --name wide-pro

# free plumbing check on any grid size: drop --llm and the deterministic mock pipeline runs at $0
uv run python -m app.backtest --tickers NVDA,AMD,MSFT --start 2025-01-01 --end 2025-12-31 --step 21 --name wide-mock
```

Notes that keep a wide run honest:

- Pick end dates far enough back that **every window matures**: last date + horizon must be in the past, or cells land in the report as pending.
- Keep temperature 0 and one run per cell; repeats measure the model's jitter, not the framework.
- `--holdout YYYY-MM-DD` splits later dates into an untouched test period, `--paired depth=fast:pro` runs a one-change A/B on the same grid, and every run writes a manifest (code revision, data hashes, seed) so numbers can be reproduced or audited later.
- On the upstream side, the same widening is just a longer CELLS list in `backtests/vs-upstream-bench/run_upstream_modal.py`; there is no free mode, so its side always costs real money.
- Treat roughly 30+ matured cells per side as the minimum before quoting a hit rate as a finding, not a teaser.

## Headline (measured this run)

| Dimension | BTA fast | BTA pro | BTA max | Upstream | 
|---|---|---|---|---|
| Agents per decision | 3 | 7 | 10 (with debates) | ~16 LLM calls |
| Wall time per decision | ~29 s (incl. cold data fetch) | ~9 s warm | ~9 s warm | ~178 s warm, 421 s cold |
| Tokens per decision | ~8.7k (est.) | ~20.3k (est.) | ~29k (est.) | ~153k (measured) |
| Cost per decision, DeepSeek Flash list | ~$0.005 | ~$0.011 | ~$0.015 | ~$0.069 (measured) |
| Six calls graded right / wrong | 1 / 5 | **2 / 4** | **2 / 4** | **2 / 4** |
| Net P&L, $10k per BUY, long-only | -$2,249 | **-$633** | **-$633** | **-$633** |

Total measured spend for this benchmark: about $0.90 (upstream ~$0.70 including an abandoned first attempt on api.deepseek.com that died mid-run with `402 Insufficient Balance` on the machine's key; BTA fast ~$0.03, pro ~$0.06, max ~$0.09 at estimator basis; the uniform rerun and pro/max all went through the same Modal endpoint BTA uses).

## Decision quality (n=6, identical grading)

P&L accounting follows the backtest harness convention: $10,000 on every BUY, long-only; SELL and HOLD sit in cash at $0. Realized returns come from closes fetched fresh for grading (one code path for both sides); BTA's own report shows slightly different realized values because its cache snapshot prices drift a little from today's adjusted closes (e.g. -1.68% vs -1.58% on NVDA 9/3). Grades are unaffected.

| Ticker | Date | BTA call | Upstream call | Realized | Alpha vs SPY | BTA P&L | Upstream P&L | BTA grade | Upstream grade |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 2026-09-03 | BUY 0.60 | Buy (Overweight) | -1.58% | -1.06% | -$158 | -$158 | wrong | wrong |
| AMD | 2026-09-03 | SELL 0.68 | Sell (Underweight) | +37.95% | +38.48% | $0 | $0 | wrong | wrong |
| MSFT | 2026-09-03 | BUY 0.60 | Buy (Overweight) | -2.39% | -1.86% | -$239 | -$239 | wrong | wrong |
| NVDA | 2026-09-25 | BUY 0.60 | Buy (Overweight) | -4.86% | -3.81% | -$486 | -$486 | wrong | wrong |
| AMD | 2026-09-25 | BUY 0.60 | **Hold** | -16.16% | -15.11% | **-$1,616** | $0 | wrong | **right** |
| MSFT | 2026-09-25 | BUY 0.60 | Buy (Overweight) | +2.50% | +3.55% | +$250 | +$250 | right | right |

| Framework | Right / wrong | Hit rate | Gains | Losses | Net P&L @$10k per BUY | Avg per decision |
|---|---|---|---|---|---|---|
| BetterTradingAgents | 1 / 5 | 17% | +$250 (1 call) | -$2,499 (4 calls) | **-$2,249** | -$375 (-3.75%) |
| Upstream | 2 / 4 | 33% | +$250 (1 call) | -$883 (3 calls) | **-$633** | -$106 (-1.05%) |

Both frameworks bled on the same four cells; the entire $1,616 gap between them is the AMD 9/25 cell, where upstream stood aside and BTA bought. Two more losses that never touched cash because neither framework can short: both called AMD Sell/Underweight on 9/3 right before a +37.95% rally, which under short-enabled accounting would have cost each about $3,795 on a $10k short. Under long-only accounting it cost nothing, but the direction was wrong all the same.

Read the small sample honestly: 6 cells, one model, one market regime (a September dip followed by a strong rally; AMD swung +38% in one window and -16% in the next), single run per cell, and historical dates carry memorization risk for both sides. With the same underlying model the two frameworks agreed on 5 of 6 calls, and upstream's only divergence - standing aside on AMD on 9/25 while BTA bought - was the single difference between the scorecards and it was the right call. On this tiny sample upstream edges quality; nothing here says that generalizes. BTA's own report flags that a deterministic momentum baseline (+35%) beat both frameworks on these cells. This table says who called these 6 cells better, not which framework is permanently smarter.

## Depth tiers on the same six cells

Pro and max produced identical calls (only confidence values moved, 0.55-0.70), and those calls exactly match upstream's per-cell decisions. The one difference from fast is standing aside on AMD on 9/25 instead of buying a -16% slide.

- Depth helped: pro and max tie upstream's scorecard (2/4 right, -$633 net) where fast took -$2,249.
- Extra depth stopped helping immediately: max's three extra agents (debate rounds plus judge) changed no decision vs pro.
- Upstream spent ~16 calls, ~153k tokens and ~178 s per cell to reach the same place pro reaches with 7 calls, ~20.3k tokens and ~9 s (warm cache, same endpoint and model).
- Caveat, as always here: n=6, single run per cell, one regime, and the entire fast-vs-pro gap is one HOLD. This sample says pro/max tie upstream, not that they beat it in general.

## Clear wins for BetterTradingAgents

1. **Speed and cost**: same model, same endpoint: BTA pro reaches upstream's exact calls on these cells at about 6x less money and 20x less wall time (fast is cheaper still, max in between). Upstream spends its budget on a much larger roster (debates, risk panels) per answer.
2. **Execution exists**: BTA talks to a real paper broker (Alpaca, paper-only) with risk caps, a wash-trade guard, decision-age checks, and an autopilot loop. Upstream states in its own backtest docstring that it "is not a portfolio simulator, and must not grow one" - no orders anywhere.
3. **Risk gates**: position/invested/cash/correlation caps plus a confidence bar guard every BUY in BTA; upstream has no equivalent concept at trade time.
4. **Cost visibility**: BTA estimates and reports per-run cost in the UI and every backtest report; upstream has no usage or cost tracking at all (this benchmark had to patch its LLM client to count tokens).
5. **Free offline mode**: BTA runs a deterministic mock pipeline on live market data with zero keys and zero spend, clearly labeled; upstream has no mock mode.
6. **Backtest experiment controls**: BTA ships paired A/B runs, holdout splits, bootstrap intervals, a momentum baseline, and reproducibility manifests with data hashes. Upstream's harness (new in 0.7.x) runs a grid and scores ratings, without experiment controls.
7. **Product surface**: web app (run viewer with SSE, portfolio, accuracy page, settings UI with OS keychain), local-only network guard, setup wizard. Upstream is CLI plus saved HTML reports.

## Where upstream wins (kept honest)

- **Decision breadth per run**: it runs analyst teams, debates, and a risk panel per decision; the extra compute buys more deliberated (and on NVDA 9/3, directionally better) answers.
- **Provider breadth**: native Anthropic, Google, Azure, Bedrock, MiniMax, ChatGPT-plan and local-server support; BTA speaks OpenAI-compatible endpoints only.
- **Checkpoint resume**: upstream can resume a crashed run from the last graph node; BTA runs are timeboxed instead.
- **Test volume**: upstream ships 88 test files / ~1000 pytest cases; BTA's CI is 35 check scripts (fast offline subset).
- **Footprint**: upstream installs lighter here (227 MB / 19 declared deps vs 333 MB / 10 for BTA - BTA carries the web stack).
- **Community**: ~117k stars and an active contributor base vs a 4-month-old personal project.

## Reproduce

```
# BTA fast, then pro and max (about 4 minutes total after the first run warms the cache, ~$0.18)
uv run python -m app.backtest --tickers NVDA,AMD,MSFT --start 2026-09-03 --end 2026-09-25 --step 22 --horizon 21 --depth fast --llm --yes --name vs-upstream-llm
uv run python -m app.backtest --tickers NVDA,AMD,MSFT --start 2026-09-03 --end 2026-09-25 --step 22 --horizon 21 --depth pro --llm --yes --name vs-upstream-pro
uv run python -m app.backtest --tickers NVDA,AMD,MSFT --start 2026-09-03 --end 2026-09-25 --step 22 --horizon 21 --depth max --llm --yes --name vs-upstream-max

# upstream: runner + grader kept in backtests/vs-upstream-bench/
# install upstream in its own venv, then run_upstream_modal.py (about 12 minutes,
# ~$0.41 at DeepSeek Flash list) and grade_both.py through BTA's own grader
```

Artifacts: `backtests/report-vs-upstream-{llm,pro,max}.md` + matching manifests (BTA), `backtests/vs-upstream-bench/upstream_runs.json` (upstream decisions, timing, measured tokens) and `grade_both.py` (the shared grader).

---

Educational project - these numbers describe a simulation of rule-based or LLM pipelines, not investment advice.
