<div align="center">

<img src="static/logo-full.png" width="460" alt="BetterTradingAgents"/>

**A fast multi-agent stock research app powered by CrewAI.**

Six AI agents research your tickers in parallel, argue the bull and bear cases,
and deliver a clear **BUY / HOLD / SELL** call with the reasoning behind it.

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![CrewAI](https://img.shields.io/badge/CrewAI-multi--agent-ff6b35)](https://www.crewai.com/)
[![uv](https://img.shields.io/badge/uv-managed-de5fe9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Configuration](#configuration) · [API](#api)

</div>

---

![BetterTradingAgents home screen](docs/screenshots/home.png)

## Why this exists

TradingAgents-style research is powerful but slow and complicated. BetterTradingAgents is the
opposite: a deliberately small MVP where **speed and clarity are the product**. Type tickers,
watch the agents work live, get a decision in under a minute, track it in a demo portfolio.

- **Parallel by design**: the three researchers run concurrently, bull and bear debate in parallel, tickers run side by side. A 2-ticker analysis finishes in well under a minute.
- **Live, honest progress**: Server-Sent Events stream every agent state change (waiting, running, complete, failed) straight into the UI. No spinners, no guessing.
- **Failures don't kill runs**: if one agent fails, the Portfolio Manager is told which input is missing and still makes a call.
- **Real data, three providers**: Finnhub for company fundamentals, Olostep for news search and scraping, yfinance for price history. Indicators (SMA20/50, RSI-14, momentum, volatility) are computed in plain Python; the LLM only interprets them.
- **Any OpenAI-compatible LLM**: OpenAI, OpenRouter, DeepSeek, Qwen, GLM, local vLLM, llama.cpp. One model for all agents.
- **Zero-frontend-build**: vanilla HTML, CSS and JavaScript served by FastAPI. One Python project, one command to run.

## How it works

```
                          NVDA, AMD, META
                                |
              +-----------------+-----------------+
              |                 |                 |
              v                 v                 v
        Technical         Fundamental           News
     (SMA/RSI/momentum)  (growth/margins/PE)  (Finnhub + Olostep)
              |                 |                 |
              +-----------------+-----------------+
                                |
                     +----------+----------+
                     |                     |
                     v                     v
                   Bull                  Bear
              (strongest buy case)  (risks and downsides)
                     |                     |
                     +----------+----------+
                                |
                                v
                       Portfolio Manager
                                |
                                v
                    BUY / HOLD / SELL + confidence
                       + full reasoning trail
```

Every stage runs concurrently with `asyncio.gather`, up to 5 tickers at once.
During the run the UI shows each agent as it works:

![Live agent progress](docs/screenshots/live-analysis.png)

Each ticker expands into the full decision, the three analyst reports and the
bull/bear debate:

![Analysis results](docs/screenshots/results.png)

## Quick start

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kingabzpro/BetterTradingAgents.git
cd BetterTradingAgents

uv sync                       # install dependencies
cp .env.example .env          # add your keys (see below)

uv run uvicorn app.main:app --reload
```

Open <http://localhost:8000>, type `NVDA, AMD, META`, click **Analyze Stocks**.

> No LLM key yet? The app still runs end-to-end: agents fall back to
> rule-based "mock" mode (clearly labeled) using the same live market data.

## Configuration

Everything lives in `.env` (see [`.env.example`](.env.example)):

| Variable | Required | What it does |
|---|---|---|
| `LLM_BASE_URL` | with key | Any OpenAI-compatible endpoint (default: `https://api.openai.com/v1`) |
| `LLM_API_KEY` | recommended | Powers all six agents |
| `LLM_MODEL` | with key | e.g. `gpt-4o-mini`, `deepseek-chat`, `zai-org/GLM-5.3-Flash` |
| `LLM_TEMPERATURE` | optional | Sampling temperature (default `0.2`) |
| `LLM_TIMEOUT_SECONDS` | optional | Per-agent timeout (default `90`) |
| `LLM_REASONING_EFFORT` | optional | Provider-specific, e.g. `none` or `low` for GLM models |
| `FINNHUB_API_KEY` | optional | Fundamentals + company news (falls back to yfinance) |
| `OLOSTEP_API_KEY` | optional | News web search + article scraping fallback |
| `STARTING_CASH` | optional | Demo portfolio starting cash (default `$100,000`) |

## Demo portfolio

After a BUY recommendation, add the stock to the simulated portfolio in one
click. Positions persist in SQLite with live prices and P&L. No real money,
no broker, no order execution.

![Demo portfolio](docs/screenshots/portfolio.png)

## API

| Endpoint | Description |
|---|---|
| `POST /api/analyze` | `{"tickers": ["NVDA", "AMD"]}` starts a run, returns `{"run_id": "..."}` |
| `GET /api/runs/{run_id}` | Run status and full results |
| `GET /api/runs/{run_id}/events` | SSE stream: `ticker_started`, `ticker_data`, `agent_started`, `agent_completed`, `agent_failed`, `ticker_completed`, `analysis_completed` |
| `GET /api/portfolio` | Positions with current prices and P&L |
| `POST /api/portfolio/add` | `{"ticker": "NVDA", "quantity": 45}` adds a demo position |
| `GET /api/health` | Config and provider status |

## Project structure

```
app/
├── main.py               # FastAPI app, routes, SSE endpoint
├── config.py             # env-driven settings
├── models.py             # Pydantic models
├── workflow.py           # 3-stage parallel orchestration
├── runs.py               # in-memory run store + event fan-out
├── portfolio.py          # SQLite demo portfolio
├── agents/               # the six CrewAI agents, one file each
│   ├── technical.py      #   interprets precomputed indicators
│   ├── fundamental.py    #   reads normalized Finnhub/yfinance data
│   ├── news.py           #   Finnhub -> Olostep -> yfinance headlines
│   ├── bull.py           #   strongest buy case
│   ├── bear.py           #   risks and downsides
│   └── manager.py        #   final BUY / HOLD / SELL decision
└── tools/
    ├── market_data.py    # Finnhub + Olostep + yfinance, merged
    └── indicators.py     # SMA / RSI / momentum / volatility in pure Python

static/                   # vanilla HTML + CSS + JS, no build step
├── index.html            # analysis page
├── portfolio.html        # demo portfolio page
├── app.js                # SSE client and rendering
├── style.css             # modern dark theme in the logo's green + navy palette
├── logo.png              # logo mark (header + favicon)
└── logo-full.png         # full logo lockup
```

## Roadmap ideas

- Streaming agent thoughts while they work
- Per-agent model selection (cheap researchers, stronger manager)
- Alpaca paper trading integration
- Backtesting simple agent strategies

## Disclaimer

Educational project. **Not investment advice.** The demo portfolio is
simulated; nothing here executes real trades.

Inspired by [TradingAgents](https://github.com/TauricResearch/TradingAgents);
this is an independent, much smaller implementation.

## License

MIT
