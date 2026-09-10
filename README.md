<div align="center">

<img src="static/logo-full.png" width="700" alt="BetterTradingAgents multi-agent market intelligence"/>

**Real-time, multi-agent AI stock research and paper trading, powered by CrewAI.**

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![CrewAI](https://img.shields.io/badge/CrewAI-multi--agent-ff6b35)](https://www.crewai.com/)
[![uv](https://img.shields.io/badge/uv-managed-de5fe9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

[Highlights](#highlights) · [Quick start](#quick-start) · [Using the app](#using-the-app) · [Configuration](#configuration) · [API](#api) · [Backtesting](#backtesting)

</div>

---

BetterTradingAgents is an improved and streamlined version of the TradingAgents concept, with a
faster parallel workflow, live progress, explainable decisions, risk controls, and paper trading.

Enter up to five stock tickers. Specialized agents analyze technicals, fundamentals,
news, social sentiment, and the 5-day price forecast in parallel, bull and bear
researchers debate across a rebuttal round, and a risk-gated **BUY / HOLD / SELL** decision
comes back with a suggested position size and the full reasoning trail. Every completed call
is also remembered and graded against what the market actually did, so the Portfolio Manager
brings a track record to the next decision, not just fresh data.

![BetterTradingAgents home screen](docs/screenshots/home.png)

## Highlights

| | Feature | What it means |
|:---:|---|---|
| ⚡ | **Parallel by design** | Researchers, data fetches, and debate rounds run concurrently, and multiple tickers run side by side. |
| ⚔️ | **Real debate** | Bull and bear each get a rebuttal round to answer the other's strongest points before the call. |
| 🗣️ | **Social sentiment** | A fifth researcher reads Reddit and StockTwits chatter, and says so when the crowd is too thin to mean anything. |
| ⚖️ | **Risk-gated decisions** | BUYs are volatility-scaled and capped by per-ticker, invested, and cash-buffer limits; a forecast beyond the stock's own noise band (±1σ) downgrades the trade. Downgrades are flagged, never silent. |
| 📜 | **Learns from its calls** | Every completed decision is recorded and later graded on realized return and alpha vs SPY; the manager weighs those lessons on the next run and the results page shows the track record. |
| 💬 | **Chat with the manager** | Every finished ticker gets a follow-up chat grounded in that run's research, so you can ask personalized questions and make the call yourself. |
| 🧪 | **Walk-forward backtests** | Replay the pipeline at past dates with point-in-time data only, grade every call against SPY after costs, and compare with buy-and-hold; free mock mode by default. |
| 📡 | **Live, honest progress** | Server-Sent Events stream every agent state with per-ticker progress bars and an optional live-reasoning pane. |
| 🕘 | **Durable run history** | Completed and interrupted analyses are saved in SQLite and can be reopened from the Runs page. |
| 🛡️ | **Resilient runs** | If an agent fails, the Portfolio Manager receives the available inputs and still makes a call. |
| 📊 | **Real market data** | Finnhub, Olostep, and yfinance provide fundamentals, news, and price history; Nixtla TimeGPT optionally provides a 5-day forecast. |
| 🧠 | **Model flexibility** | Use any OpenAI-compatible LLM, split by role: a cheap fast model for the researchers, a stronger one only for the final BUY/HOLD/SELL call. |
| 🪶 | **No frontend build step** | Vanilla HTML, CSS, and JavaScript are served directly by FastAPI. |

## How it works

```mermaid
flowchart LR
    T["📈 Tickers<br/>up to five per run"] --> D["📡 Market data<br/>yfinance · Finnhub · Olostep · TimeGPT"]

    subgraph RESEARCH["🔬 Research: five analysts in parallel"]
        direction TB
        TA["Technical<br/>SMA · RSI · MACD · volume"]
        FA["Fundamentals<br/>growth · margins · valuation"]
        NA["News<br/>headlines · catalysts"]
        SA["Sentiment<br/>Reddit · StockTwits chatter"]
        FC["Forecast<br/>TimeGPT vs own noise band"]
        TA ~~~ FA ~~~ NA ~~~ SA ~~~ FC
    end

    subgraph DEBATE["⚔️ Debate: bull vs bear"]
        direction TB
        BULL["🐂 Bull researcher<br/>strongest case to buy"]
        RB["Rebuttal round<br/>each side answers the other"]
        BEAR["🐻 Bear researcher<br/>risks and downsides"]
        BULL --- RB --- BEAR
    end

    D --> RESEARCH
    RESEARCH --> DEBATE
    DEBATE --> PM["👔 Portfolio Manager<br/>weighs debate, holdings, track record"]
    TRACK["📜 Track record<br/>decision memory + walk-forward backtests<br/>calls graded vs SPY after costs"] --> PM
    PM --> RISK["🛡️ Risk gate<br/>vol-scaled size · exposure caps · forecast check"]
    RISK --> RESULT["✅ BUY · HOLD · SELL<br/>confidence + size + reasoning trail"]
    RESULT --> CHAT["💬 Chat with the manager<br/>grounded in this run, you decide"]
```

The whole pipeline runs concurrently, tickers included, and ends in a conversation: the
manager's call is a starting view, and the per-ticker chat helps you reach your own decision.

## Quick start

You need [Python 3.12+](https://www.python.org/) and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kingabzpro/BetterTradingAgents.git
cd BetterTradingAgents
uv sync        # install dependencies
uv run setup   # interactive wizard: pick a provider, paste keys, done
uv run app     # start the app on http://127.0.0.1:8000
```

Three commands, and the wizard does the configuration for you: it offers the common LLM
providers as presets (OpenAI, Z.AI, DeepSeek, Qwen, OpenRouter, or any custom
OpenAI-compatible endpoint), hides your API key while typing, optionally tests it with a
one-token request, collects the optional market-data keys, and writes `.env`. Ctrl+C at any
prompt writes nothing.

> [!TIP]
> No LLM key yet? Leave `LLM_API_KEY` empty. The complete workflow remains available in clearly
> labeled rule-based mock mode using live market data.

Prefer configuring by hand? Copy [`.env.example`](.env.example) to `.env` and edit it; every
setting is optional and documented in [Configuration](#configuration). Keys stay in `.env`,
which is gitignored; restart the app after changing it.

### Recommended models

> [!IMPORTANT]
> For the best balance of **price, accuracy, and speed**, start with DeepSeek V4 Flash,
> Qwen3.8 Flash, or GLM-5.3-Flash. Provider pricing and availability can vary by region.

| Provider | `LLM_MODEL` value | Why choose it |
|---|---|---|
| [DeepSeek](https://api-docs.deepseek.com/quick_start/pricing) | `deepseek-v4-flash` | Fast, cost-efficient general reasoning for multi-agent runs |
| [Alibaba Cloud Qwen](https://www.alibabacloud.com/help/en/model-studio/getting-started/models) | `qwen3.8-flash` | High-speed model with strong instruction following and a large context window |
| [Z.AI](https://docs.z.ai/guides/vlm/glm-5.3-flash) | `glm-5.3-flash` | Efficient reasoning with strong quality at a lower serving cost |

### Per-role models

Set `LLM_MODEL` to the cheap fast model, then override the manager so the final judgment runs
on the stronger one. Each role can also point at its own endpoint and key
(`LLM_BASE_URL_*` / `LLM_API_KEY_*`):

```env
LLM_MODEL=zai-org/GLM-5.3-Flash
LLM_MODEL_MANAGER=zai-org/GLM-5.3
```

## Using the app

### Run your first analysis

Open [http://localhost:8000](http://localhost:8000), add tickers such as `NVDA, AMD, META`
(one at a time, paste several at once, or use the quick-add chips, up to 5), pick your
outlook (**Day trading**, **Short term**, or **Long term**) and depth (**Fast** = technical +
news + single debate round · **Medium** = all researchers · **Expert** = adds the bull/bear
rebuttal round), then select **Analyze Stocks**. The outlook is sent to every agent, so they
all weigh evidence for the horizon you actually trade; the depth trades thoroughness for speed.

Watch each agent move from waiting to running to complete, with a progress bar per ticker.
If an agent fails, its slot says so and the manager still makes a call on the inputs that
survived.

![Live agent progress](docs/screenshots/live-analysis.png)

### Read the results

The summary row shows the final call with its evidence-strength label, current price, your
horizon, data age (stale and cached states included), analyst coverage with a compact signal
split such as `3 bullish / 1 neutral / 1 bearish`, the risk summary, and the estimated model
cost of the run. When the deterministic risk gate overrides the manager, the call renders as
`Manager: BUY -> Final: HOLD` with the exact flag that caused it.

Expand any ticker to read the decision brief in one order: manager conclusion, the conditions
that would change the call (`would_upgrade_if` / `would_downgrade_if`, labeled as conditions,
not alerts), risk changes, the bull-versus-bear debate including rebuttals, analyst evidence,
sources with links, and the track record of previous calls graded against SPY.

![Analysis results](docs/screenshots/results.png)

### Chat with the Portfolio Manager

Once a ticker finishes, its result card gets a **Chat with Portfolio Manager** button that
opens a per-ticker conversation grounded in that run's research, the debate, and your current
holdings. Ask anything personalized, such as whether the stock fits goals beyond this
portfolio, what would change the call, or which risk matters most. The manager answers in
plain prose, quotes the numbers from the run, and says plainly when a question reaches beyond
the research.

### I Am Feeling Lucky discovery

Select **I Am Feeling Lucky** when you want the app to find research candidates automatically.
The backend screens U.S.-listed companies (market cap between $1B and $50B, at least $100M
trailing revenue with 10% growth, and over 500,000 average daily shares traded), then ranks
them with a momentum score grounded in the published literature: 3-6 month formation momentum
that skips the most recent month, path smoothness, 52-week-high proximity, and volatility
scaling. Your chosen outlook adjusts the formation weights; the top five candidates go
straight into the ticker input and through the normal workflow. The screen is cached for an
hour, and the ranking is a research starting point, not a promise.

### Portfolio: your own holdings + paper trading

The portfolio page tracks two kinds of positions in one SQLite-backed book:

- **Tracked holdings**: shares you already own, added by ticker, quantity, and price paid
  (or imported from CSV with a row-by-row preview). They are valued at live prices and roll
  into P&L, but never touch the simulated cash balance.
- **Demo trades**: after a **BUY** recommendation, add the stock to the simulated portfolio
  in one click. Demo buys and closes move the simulated cash.

The Portfolio Manager sees all open positions when making its next call, and the risk gate's
exposure caps use the combined equity. No broker is connected and no real orders are placed.

![Demo portfolio](docs/screenshots/portfolio.png)

### Run history

Every analysis is saved and listed newest-first on the **Runs** page, scoped to an anonymous
ID in your browser. A direct `?run=<id>` link reopens a specific result even after a server
restart. A running analysis can be cancelled from the live view (finished tickers are kept),
and the Runs page can rerun any finished run with its original tickers, outlook, and depth.

### Data honesty

Forecasts are supporting evidence, never targets: each one is assessed against the ±1σ
five-day move implied by the stock's own volatility, and the forecast card says so. Social
sentiment with fewer than three posts reads as neutral with low confidence, never as a
signal. Cached results keep their original timestamp and are labeled as cached. Every result
card shows an estimated model cost at provider list prices, with `cost unknown` when a model
has no known price.

## Configuration

Run `uv run setup` to configure interactively, or copy [`.env.example`](.env.example) to
`.env` and override only what you need. Every setting is optional; without an LLM key, the
app starts in mock mode. The groups below mirror the sections of `.env.example`, and the
defaults live in `app/config.py`.

### LLM

Any OpenAI-compatible endpoint. Pair a cheap fast model with the researchers and a stronger one
with the manager via the per-role overrides.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API endpoint |
| `LLM_API_KEY` | Not set | Enables the LLM agents (researchers, debaters, manager) |
| `LLM_MODEL` | `gpt-5.6-luna` | Model used by every agent without a per-role override |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `LLM_TIMEOUT_SECONDS` | `90` | Timeout for each agent call |
| `LLM_REASONING_EFFORT` | Not set | Optional provider-specific reasoning effort (e.g. `none`/`low` for GLM) |
| `LLM_PRICE_IN` / `LLM_PRICE_OUT` | `0` | Cost-estimate override in USD per 1M input/output tokens for every role (custom/proxied pricing); `0` uses the built-in list-price table in `app/cost.py` |
| `LLM_MODEL_MANAGER` | `LLM_MODEL` | Per-role model for the final BUY/HOLD/SELL call |
| `LLM_BASE_URL_MANAGER` / `LLM_API_KEY_MANAGER` | global values | Optional endpoint/key just for the manager |
| `LLM_MODEL_ANALYSTS` (+ `_BASE_URL_` / `_API_KEY_`) | global values | Per-role overrides for the 5 researchers |
| `LLM_MODEL_DEBATE` (+ `_BASE_URL_` / `_API_KEY_`) | global values | Per-role overrides for the bull/bear debaters |

### Market-data providers

All optional; each has a built-in fallback.

| Variable | Default | Purpose |
|---|---|---|
| `FINNHUB_API_KEY` | Not set | Company profiles, fundamentals, and news; falls back to yfinance |
| `OLOSTEP_API_KEY` | Not set | News search/scraping fallback and Reddit/StockTwits sentiment search |
| `NIXTLA_API_KEY` | Not set | Nixtla TimeGPT 5-day forecast; falls back to the local trend model |

### Analysis

| Variable | Default | Purpose |
|---|---|---|
| `MAX_TICKERS` | `5` | Maximum tickers accepted in one analysis |
| `DEBATE_ROUNDS` | `2` | Bull/bear debate depth: `1` = single round, `2`+ adds one rebuttal exchange (capped at 3) |
| `STREAM_REASONING` | `0` | Live reasoning stream: `1` streams agent tokens to the UI (off by default: the stream is mostly the final JSON and reads as noise) |

### Demo portfolio and storage

| Variable | Default | Purpose |
|---|---|---|
| `STARTING_CASH` | `100000` | Initial simulated portfolio balance |
| `DEFAULT_POSITION_SIZE` | `10000` | Suggested position value |
| `DB_PATH` | `portfolio.db` | SQLite app database path for portfolio positions and run history |

### Risk gate

Fractions of total equity applied to every BUY.

| Variable | Default | Purpose |
|---|---|---|
| `MAX_POSITION_PCT` | `0.10` | Max fraction of equity in one ticker |
| `MAX_INVESTED_PCT` | `0.60` | Max fraction of equity invested |
| `MIN_CASH_PCT` | `0.10` | Min cash buffer after a BUY |

### Decision memory and calibration

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_HORIZON_DAYS` | `21` | Days a past call is held before its realized-return grade is final |
| `MEMORY_REFLECT_WITH_LLM` | `0` | `1` asks the LLM for reflection lessons instead of deterministic sentences |
| `CALIBRATION_MIN_OBSERVATIONS` | `30` | Minimum mature graded decisions before a confidence bucket shows a track record instead of `Track record unavailable` |

### Backtests

| Variable | Default | Purpose |
|---|---|---|
| `BACKTEST_CACHE` | `docs/backtests/cache.db` | SQLite snapshot cache location (gitignored) |
| `BACKTEST_OFFLINE` | `0` | `1` makes cache misses fail instead of hitting the network (proves warm re-runs are truly offline) |

## API

| Method | Route | Purpose |
|:---:|---|---|
| `POST` | `/api/analyze` | Start an analysis run for one or more tickers |
| `GET` | `/api/discover` | Rank liquid growth companies with a research-grounded momentum score and return up to five candidates |
| `GET` | `/api/runs` | List saved analysis runs, newest first |
| `DELETE` | `/api/runs` | Clear the current browser's finished run history |
| `GET` | `/api/runs/{run_id}` | Read run status and complete results |
| `GET` | `/api/runs/{run_id}/events` | Stream live progress over SSE |
| `POST` | `/api/runs/{run_id}/cancel` | Cancel a running analysis, keeping finished ticker results |
| `POST` | `/api/runs/{run_id}/chat` | Ask the portfolio manager follow-up questions about one ticker |
| `GET` | `/api/portfolio` | List positions with live prices and profit/loss |
| `POST` | `/api/portfolio/add` | Add a simulated position |
| `POST` | `/api/portfolio/import` | Record tracked holdings (manual entry / CSV import) |
| `POST` | `/api/portfolio/close` | Close a position at the live (or given) price and realize P/L |
| `GET` | `/api/health` | Check configuration and provider status |

<details>
<summary><strong>Example: start an analysis</strong></summary>

```bash
curl -X POST http://localhost:8000/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"tickers":["NVDA","AMD"]}'
```

</details>

Every endpoint returns documented Pydantic models; see `app/models.py` for the full result
schema (pre-gate manager call, change conditions, data quality, forecast, per-agent reports,
risk output, and the cost estimate).

## Backtesting

The walk-forward harness answers "is the pipeline better than buy-and-hold?" It replays the
full analysis at each grid date using only data known at that date:

```bash
uv run python -m app.backtest --tickers NVDA,AMD,META --start 2026-03-01 --end 2026-06-30 --step 21
```

- **Point-in-time data**: OHLCV and news are cut off at each decision date; fundamentals and
  social posts have no point-in-time source, so those analysts replay honestly (excluded and
  thin-neutral respectively) instead of leaking present-day data. Portfolio context and
  decision memory are disabled during replay.
- **Grading with costs**: BUY earns the window's return minus a 2×5bp round-trip cost, each
  call compared with SPY over the same window; reports add hit rate, cumulative return,
  Sharpe, max drawdown, and an all-HOLD / buy-and-hold / deterministic-momentum baseline set.
- **Honest experiments**: `--holdout YYYY-MM-DD` splits tune and untouched test dates with
  bootstrap intervals; `--paired depth=fast:expert` (also `rebuttals`, `forecast`,
  `sentiment`, `model`) changes exactly one dimension at a time. Verdicts refuse to promote
  thin or inconclusive samples.
- **Reproducible**: every report carries a manifest (code revision, policy version, config,
  snapshot hashes, seeds); mock mode is the free default, and `--llm` flags its result
  `memorization_risk: high`.

Reports land in [`docs/backtests/`](docs/backtests/) as JSON + markdown, and warm-cache
re-runs make zero network calls.

## Development

```bash
uv run test         # fast offline suite (quick wins, cost, run history, setup wizard)
uv run test chat    # one specific group
uv run test all     # every group, including the browser smoke test

uv run python scripts/smoke_llm.py        # one-shot: does the configured LLM answer?
uv run python -m app.calibration           # regenerate the calibration report, no LLM calls
```

Every check is a standalone script in `scripts/` with a docstring explaining what it covers;
`check_risk.py` runs a full mock analysis end-to-end and needs network access for market data.

```
app/            FastAPI app: main routes, workflow pipeline, agents, tools,
                risk gate, chat, portfolio, run history, memory, backtests
static/         vanilla HTML + ES-module JS + per-page CSS, no build step
scripts/        offline checks + the setup wizard (uv run test / uv run setup)
docs/           roadmap, accessibility protocol, backtest reports
```

## Roadmap

The detailed, research-backed plan lives in [docs/ROADMAP.md](docs/ROADMAP.md). Shipped so
far: the decision brief with trust state, run controls, the accessibility pass, historical
confidence calibration, and the honest experiment workflow. Next up: portfolio concentration
risk, a watchlist with decision-change tracking, decision comparison with price context,
search and export, and Alpaca paper trading.

## Disclaimer

> [!WARNING]
> BetterTradingAgents is an educational project, not investment advice. The portfolio is simulated;
> nothing in this project executes real trades.

This project is an improved and streamlined implementation inspired by
[TradingAgents](https://github.com/TauricResearch/TradingAgents).

## License

MIT
