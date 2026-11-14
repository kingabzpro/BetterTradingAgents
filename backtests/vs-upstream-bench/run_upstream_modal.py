"""Rerun the full upstream grid on the exact endpoint/model BTA used.

Loads BTA's resolved LLM settings at runtime (never printed), points upstream's
"openai" provider at that base_url, and rewrites upstream_runs.json so every
cell shares one endpoint, one model, and one token ledger.
"""

import json
import os
import sys
import time

sys.path.insert(0, r"C:\Repository\GitHub\BetterTradingAgents")
sys.path.insert(0, r"C:\Windows\Temp\ta-bench")

from app.config import settings

os.environ["OPENAI_API_KEY"] = settings.llm_api_key

import run_upstream as ru  # noqa: E402  (installs the usage patch + faulthandler)

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402
from tradingagents.graph.trading_graph import TradingAgentsGraph  # noqa: E402

OUTFILE = ru.OUTFILE
ENDPOINT = settings.llm_base_url
MODEL = settings.llm_model


def make_config(ticker: str, date: str) -> dict:
    cfg = DEFAULT_CONFIG.copy()
    cfg.update({
        "llm_provider": "openai",
        "backend_url": ENDPOINT,
        "quick_think_llm": MODEL,
        "deep_think_llm": MODEL,
        "temperature": 0.0,
        "holding_period_days": 21,
        "benchmark_ticker": "SPY",
        "memory_log_path": str(ru.BASE / f"memory_{ticker}_{date}.md"),
    })
    return cfg


def main() -> None:
    out = {"cells": [], "endpoint": ENDPOINT, "model": MODEL}
    for ticker, date in ru.CELLS:
        row = {"ticker": ticker, "date": date}
        before = dict(ru.USAGE)
        t0 = time.time()
        try:
            ta = TradingAgentsGraph(debug=False, config=make_config(ticker, date))
            _, decision = ta.propagate(ticker, date)
            row["decision"] = decision
            row["ok"] = True
        except Exception as exc:
            row["ok"] = False
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["seconds"] = round(time.time() - t0, 1)
        row["calls"] = ru.USAGE["calls"] - before["calls"]
        row["input_tokens"] = ru.USAGE["input_tokens"] - before["input_tokens"]
        row["output_tokens"] = ru.USAGE["output_tokens"] - before["output_tokens"]
        out["cells"].append(row)
        print(f"[bench2] {ticker} {date}: ok={row['ok']} {row['seconds']}s "
              f"calls={row['calls']} tok_in={row['input_tokens']} tok_out={row['output_tokens']}",
              flush=True)
        OUTFILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"[bench2] DONE totals={json.dumps(ru.USAGE)}", flush=True)


if __name__ == "__main__":
    main()
