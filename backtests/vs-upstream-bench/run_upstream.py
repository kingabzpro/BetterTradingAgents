"""Benchmark runner for upstream TradingAgents.

Runs a fixed (ticker, date) grid with the same model BTA uses (deepseek-flash,
temperature 0), one fresh graph per cell so cells stay independent, and counts
tokens by patching LangChain's BaseChatModel (upstream tracks no usage itself).
"""

import faulthandler
import json
import os
import pathlib
import time

# never let a provider call hang the whole benchmark
os.environ.setdefault("OPENAI_TIMEOUT", "420")
os.environ.setdefault("OPENAI_MAX_RETRIES", "2")

BASE = pathlib.Path(r"C:\Windows\Temp\ta-bench")
faulthandler.dump_traceback_later(180, repeat=True)
os.environ["TRADINGAGENTS_RESULTS_DIR"] = str(BASE / "logs")
os.environ["TRADINGAGENTS_CACHE_DIR"] = str(BASE / "cache")

from langchain_openai.chat_models.base import ChatOpenAI

USAGE = {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def _harvest(res) -> None:
    for g in res.generations:
        um = getattr(g.message, "usage_metadata", None)
        if um:
            USAGE["input_tokens"] += um.get("input_tokens") or 0
            USAGE["output_tokens"] += um.get("output_tokens") or 0
            USAGE["calls"] += 1


_orig_generate = ChatOpenAI._generate


def _generate(self, messages, stop=None, run_manager=None, **kw):
    res = _orig_generate(self, messages, stop=stop, run_manager=run_manager, **kw)
    _harvest(res)
    return res


ChatOpenAI._generate = _generate

_orig_agenerate = ChatOpenAI._agenerate


async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
    res = await _orig_agenerate(self, messages, stop=stop, run_manager=run_manager, **kw)
    _harvest(res)
    return res


ChatOpenAI._agenerate = _agenerate

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

CELLS = [(t, d) for d in ("2026-09-03", "2026-09-25") for t in ("NVDA", "AMD", "MSFT")]
OUTFILE = BASE / "upstream_runs.json"


def make_config(ticker: str, date: str) -> dict:
    cfg = DEFAULT_CONFIG.copy()
    cfg.update({
        "llm_provider": "deepseek",
        "quick_think_llm": "deepseek-flash",
        "deep_think_llm": "deepseek-flash",
        "temperature": 0.0,
        "holding_period_days": 21,
        "benchmark_ticker": "SPY",
        # per-cell memory log keeps cells independent (BTA cells are too)
        "memory_log_path": str(BASE / f"memory_{ticker}_{date}.md"),
    })
    return cfg


def main() -> None:
    out = {"cells": []}
    for ticker, date in CELLS:
        row = {"ticker": ticker, "date": date}
        before = dict(USAGE)
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
        row["calls"] = USAGE["calls"] - before["calls"]
        row["input_tokens"] = USAGE["input_tokens"] - before["input_tokens"]
        row["output_tokens"] = USAGE["output_tokens"] - before["output_tokens"]
        out["cells"].append(row)
        print(f"[bench] {ticker} {date}: ok={row['ok']} {row['seconds']}s "
              f"calls={row['calls']} tok_in={row['input_tokens']} tok_out={row['output_tokens']}",
              flush=True)
        OUTFILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"[bench] DONE totals={json.dumps(USAGE)}", flush=True)


if __name__ == "__main__":
    main()
