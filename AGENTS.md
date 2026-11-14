# AGENTS.md

BetterTradingAgents - a FastAPI + OpenAI Agents SDK multi-agent stock research and paper-trading app.
Python 3.12+, dependencies managed by `uv`. No frontend build step (vanilla HTML/CSS/JS in `static/`).

## Commands

- `uv sync` — install/refresh dependencies from `uv.lock`.
- `uv run app` — start the single-process server at `127.0.0.1:8000`; it tries the next free port if needed. **No autoreload:** stop and restart it after code changes.
- `uv run test` — fast offline subset; the selection is in `app/cli.py`.
- `uv run test <name> [name ...]` — run specific checks, e.g. `uv run test settings config_precedence`.
- `uv run test offline` — all checks except the browser smoke test; this is the CI test selection.
- `uv run test all` — all checks, including the Playwright browser smoke test. The smoke test skips if Playwright or a system Chromium browser is unavailable.
- `uv run ruff check app scripts` — CI lint check.
- `uv run python scripts/smoke_llm.py` — one live LLM call; it can incur charges.
- `uv run python -m app.backtest --tickers NVDA,AMD --start 2026-03-01 --end 2026-06-30 --step 21` — walk-forward backtest; check options and cost before running with live LLMs.

## Layout

- `app/main.py` owns the FastAPI routes, startup, local-only guard, and SSE endpoint; `app/runs.py` manages active runs and `app/run_history.py` persists history.
- `app/workflow.py` orchestrates research, debate, and manager calls; `app/agents/` contains agent instructions and result parsing. Shared response models live in `app/models.py`.
- `app/tools/` owns market data and indicators; `app/risk.py` and `app/broker.py` own risk checks and paper orders. `app/backtest/` contains the walk-forward harness.
- `app/config.py` loads configuration and resolves per-agent/role LLM settings; `app/settings_store.py` owns the Settings field spec, persistence, and live updates.
- `scripts/check_*.py` are standalone checks. `scripts/setup_wizard.py`, `benchmark.py`, and `accuracy.py` are utilities.
- `static/` is served directly; `static/js/run.js` consumes run events. Shared navigation is rendered through `app/pages.py`.
- `portfolio.db` and generated `backtests/` reports, manifests, and caches are user data. Do not overwrite or delete them as part of routine development.

## Branching

- `dev` is the long-lived integration and experiment branch: branch `feat/`, `fix/`, `docs/`, `exp/` off it and merge back into it; it PRs into `main` whenever a batch is ready.
- `main` is protected: merges need a PR with green CI (admins exempt). See CONTRIBUTING.md for the full flow.

## Conventions

- Add behavior in the narrowest module that already owns it; match the surrounding style (typed, small functions, docstrings that explain *why*).
- Checks run in separate processes and set up their own scratch state before importing app modules. Never import two checks into one process. Add checks as `scripts/check_<area>.py`; they are included by `test offline` and `test all`, but not automatically in the fast subset.
- Use `uv run test <name>` instead of running checks directly: the runner sets `PYTHON_DOTENV_DISABLED=1` so real `.env` settings cannot replace scratch database paths or credentials. Isolate the keychain too when a check touches saved settings.
- Settings priority per configured field: `.env` → process environment → in-app Settings/keychain → `app/config.py` defaults. Blank values allow saved fallbacks; file/environment fields are locked in Settings and need a restart after edits. Other Settings apply live. Keys saved through Settings use the OS keychain, with an unencrypted local DB fallback when unavailable.
- LLM configuration also resolves per agent → role → global via `settings.llm_for`; preserve `safe_base_url` validation because API keys follow the configured endpoint.
- **Secrets:** never read or edit the real `.env`; use `.env.example` for the variable list. Never print or commit credentials. Get owner sign-off before live LLM backtests or other paid experiments.
- **Paper trading only** — the Alpaca client is hardcoded to `paper=True`. Do not add live-trading paths.
- The server binds to `127.0.0.1` and checks the socket peer, `Host`, and `Origin`; keep those guards intact.
- Keep server SSE event payloads and their frontend consumers in sync; use the existing rendering and shared UI helpers for frontend changes.

## Verify before claiming done

Run the narrowest relevant `uv run test <name>` or `uv run test` and report the result. For broader changes, run the CI checks (`uv run ruff check app scripts` and `uv run test offline`). Report skipped checks and failures; do not claim a fix or feature works without a green run.
