"""Offline checks for the live reasoning stream (ROADMAP 3.2).

Run: PYTHONPATH=. uv run python scripts/check_streaming.py
No network: LLM objects are built (never called), the sink is driven with
synthetic events, and the e2e injects an in-memory MarketData snapshot in mock
mode (with a live context, so the mock word-by-word stream exercises the same
path the UI reads).
"""

import asyncio
import os
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "streaming_test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["OLOSTEP_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
os.environ["NIXTLA_API_KEY"] = ""

from app import workflow  # noqa: E402
from app.config import settings  # noqa: E402
from app.runs import Run  # noqa: E402
from app.tools.market_data import MarketData  # noqa: E402

# ---- config flag ---------------------------------------------------------------
assert settings.stream_reasoning is True
print("STREAM_REASONING default on OK")

# ---- failure classification: stream rejections are their own bucket -------------
assert workflow._classify_failure(
    RuntimeError("stream is not supported by this provider")
) == "stream"
assert workflow._classify_failure(
    RuntimeError("stream mode unsupported with response_format json_object")
) == "stream", "combined rejection must drop streaming first"
assert workflow._classify_failure(RuntimeError("response_format is not supported")) == "response_format"
assert workflow._classify_failure(RuntimeError("HTTP 429 - rate limit exceeded")) == "rate_limit"
assert workflow._classify_failure(ValueError("no JSON object in model output: 'x'")) == "bad_output"
print("failure classification OK: stream bucket precedes response_format")


# ---- LLM objects carry the stream flag; fallback drops it ------------------------
async def llm_flag_checks():
    settings.llm_api_key = "test-key"
    settings.llm_model = "fast-model"
    workflow._llms.clear()
    workflow._llm_roles_initialized.clear()
    on = workflow.get_llm("analysts")
    assert getattr(on, "stream", None) is True, getattr(on, "stream", None)
    replacement = workflow._drop_stream_mode("analysts")
    assert replacement is on or getattr(replacement, "stream", None) is False
    assert "response_format" in (replacement.additional_params or {}), "JSON mode preserved"
    # The JSON-mode drop keeps the stream setting from settings (still on).
    rebuilt = workflow._drop_json_mode("analysts")
    assert "response_format" not in (rebuilt.additional_params or {})
    settings.llm_api_key = ""
    workflow._llms.clear()
    workflow._llm_roles_initialized.clear()


asyncio.run(llm_flag_checks())
print("LLM stream flag OK: on by default, dropped on rejection, JSON mode preserved")


# ---- token sink: forwards content chunks only, caps runaway volume ---------------
events: list[dict] = []


async def emit(kind, payload):
    events.append({"type": kind, **payload})


async def sink_checks():
    sink = workflow._token_sink("NVDA", "news", emit)

    def chunk(text):
        sink(None, SimpleNamespace(type="llm_stream_chunk", chunk=text))

    # Non-chunk events (thinking deltas, call lifecycle) never forward.
    sink(None, SimpleNamespace(type="llm_thinking_chunk", chunk="secret"))
    sink(None, SimpleNamespace(type="llm_call_started"))
    sink(None, SimpleNamespace(type="llm_stream_chunk", chunk=""))
    await asyncio.sleep(0.05)
    assert events == [], events
    # Chunks forward in order while the loop is running.
    chunk("buy ")
    chunk("signal")
    await asyncio.sleep(0.05)
    assert [e["text"] for e in events] == ["buy ", "signal"], events
    assert all(e["type"] == "agent_token" and e["ticker"] == "NVDA"
               and e["agent"] == "news" for e in events)
    # Chunks arriving on a WORKER thread still forward (crewai runs parts of
    # kickoff in executors; get_running_loop() raises there - the original bug).
    import threading

    events.clear()
    worker = threading.Thread(
        target=chunk, args=("from another thread",), daemon=True
    )
    worker.start()
    worker.join()
    await asyncio.sleep(0.05)
    assert [e["text"] for e in events] == ["from another thread"], events
    # Past the cap, forwarding stops and the last event is flagged truncated.
    events.clear()
    chunk("x" * workflow.STREAM_MAX_CHARS)
    chunk("one chunk too many")
    await asyncio.sleep(0.05)
    forwarded = sum(len(e["text"]) for e in events)
    assert forwarded == workflow.STREAM_MAX_CHARS, forwarded
    assert events[-1]["truncated"] is True
    # The sink's sliding window keeps only the last STREAM_WINDOW_CHARS.
    sink2 = workflow._token_sink("AMD", "manager", emit)
    sink2(None, SimpleNamespace(type="llm_stream_chunk", chunk="a" * 5000))
    state = next(
        c.cell_contents for c in sink2.__closure__ if isinstance(c.cell_contents, dict)
    )
    assert len(state["window"]) == workflow.STREAM_WINDOW_CHARS, len(state["window"])
    await asyncio.sleep(0.05)


asyncio.run(sink_checks())
print("token sink OK: content-only, ordered, cross-thread, capped at",
      workflow.STREAM_MAX_CHARS, "chars")

# ---- Run.emit: token events are live-only ----------------------------------------
async def run_emit_checks():
    run = Run(["NVDA"])
    queue: asyncio.Queue = asyncio.Queue()
    run.queues.append(queue)
    await run.emit("agent_started", {"ticker": "NVDA", "agent": "news"})
    await run.emit("agent_token", {"ticker": "NVDA", "agent": "news", "text": "hi"})
    types_in_store = [e["type"] for e in run.events]
    assert types_in_store == ["agent_started"], types_in_store
    queued = [queue.get_nowait() for _ in range(2)]
    assert [e["type"] for e in queued] == ["agent_started", "agent_token"], queued


asyncio.run(run_emit_checks())
print("Run.emit OK: agent_token broadcast live but never persisted for replay")


# ---- hermetic e2e: mock pipeline streams word-by-word, in order -------------------
def snapshot(ticker: str) -> MarketData:
    random.seed(23)
    closes = [100.0]
    for _ in range(129):
        closes.append(closes[-1] * (1 + random.uniform(-0.015, 0.02)))
    return MarketData(
        ticker=ticker,
        price=closes[-1],
        company_name="Nvidia Corp",
        closes=closes,
        highs=[c * 1.01 for c in closes],
        lows=[c * 0.99 for c in closes],
        volumes=[1_000_000.0] * len(closes),
        fundamentals={"pe_ratio_ttm": 35.0},
        news=[{"title": "Guidance raised", "source": "finnhub", "summary": "beat",
               "url": "", "published": ""}],
        social=[],
        sources={"prices": "yfinance", "fundamentals": "none",
                 "news": "none", "social": "none"},
        as_of="2026-09-04T20:00:00+00:00",
    )


async def e2e():
    stream_events: list[dict] = []

    async def emit(kind, payload):
        stream_events.append({"type": kind, **payload})

    # live_context=True (with an isolated empty DB) so the mock token stream runs.
    result = await asyncio.wait_for(
        workflow.analyze_ticker("NVDA", emit, market_data=snapshot("NVDA"),
                                live_context=True),
        timeout=90,
    )
    assert result.error is None, result.error
    tokens = [e for e in stream_events if e["type"] == "agent_token"]
    assert tokens, "no agent_token events in mock e2e"
    assert all(e.get("text") for e in tokens)
    # Per agent: started < first token < completed, and the streamed text
    # reassembles into the mock summary the result card shows.
    for agent in ("technical", "news", "sentiment", "bull", "bear", "manager"):
        idx_started = next(
            i for i, e in enumerate(stream_events)
            if e["type"] == "agent_started" and e["agent"] == agent
        )
        idx_completed = next(
            i for i, e in enumerate(stream_events)
            if e["type"] == "agent_completed" and e["agent"] == agent
        )
        agent_tokens = [
            e for e in tokens
            if e["agent"] == agent and idx_started < stream_events.index(e) < idx_completed
        ]
        assert agent_tokens, f"{agent} streamed no tokens between started/completed"
        streamed = "".join(e["text"] for e in agent_tokens)
        final = {
            "technical": result.technical, "news": result.news,
            "sentiment": result.sentiment, "bull": result.bull,
            "bear": result.bear, "manager": None,
        }[agent]
        expected = final.summary if final else result.summary
        assert streamed.replace(" ", "") == expected.replace(" ", ""), (agent, streamed[:80])
    print("e2e OK:", result.decision, "|", len(tokens), "token events across",
          len({e["agent"] for e in tokens}), "agents, summaries reassemble")

    # Backtest replay (live_context=False): no token events at all.
    stream_events.clear()
    await asyncio.wait_for(
        workflow.analyze_ticker("AMD", emit, market_data=snapshot("AMD"),
                                live_context=False),
        timeout=90,
    )
    assert not [e for e in stream_events if e["type"] == "agent_token"]
    print("backtest mode OK: no cosmetic token events during replay")


asyncio.run(e2e())
print("ALL STREAMING CHECKS PASSED")
