/* SSE event handling: live agent progress -> progress UI updates. */

import { AGENTS, STREAM_WINDOW_CHARS } from "./constants.js?v=11";
import { state } from "./state.js?v=11";
import { $ } from "./util.js?v=11";
import { labelFor, renderResultCard, renderSummaryTable, setAgentStatus, setHeader } from "./render.js?v=13";

export function handleEvent(event) {
  const { ticker } = event;
  if (!ticker || !state.tickers.has(ticker)) return;
  const entry = state.tickers.get(ticker);
  switch (event.type) {
    case "ticker_data":
      setHeader(ticker, event.price, event.company_name, event.sources);
      break;
    case "agent_token": {
      // Live reasoning stream (server sends only while the agent runs).
      const cell = $(`cell-${ticker}-${event.agent}`);
      const pre = $(`stream-pre-${ticker}-${event.agent}`);
      if (!cell || !pre) break;
      pre.textContent = (pre.textContent + String(event.text || "")).slice(-STREAM_WINDOW_CHARS);
      if (event.truncated && !cell.classList.contains("truncated")) {
        pre.textContent += "\n…stream truncated (output continued; display window capped)";
        cell.classList.add("truncated");
      }
      if (!cell.classList.contains("has-stream")) {
        cell.classList.add("has-stream");
        const row = cell.querySelector(".agent-row");
        const label = AGENTS.find((a) => a.key === event.agent)?.label || event.agent;
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        row.setAttribute("aria-expanded", "false");
        row.setAttribute("aria-label", `${label} live reasoning, press Enter to toggle`);
        const hint = row.querySelector(".stream-hint");
        if (hint) hint.hidden = false;
      }
      const pane = $(`stream-${ticker}-${event.agent}`);
      if (pane && !pane.hidden) pane.scrollTop = pane.scrollHeight;
      break;
    }
    case "agent_started":
      entry.runningAgents?.add(event.agent);
      setAgentStatus(ticker, event.agent, "running", "Running…");
      break;
    case "agent_completed": {
      const resultLabel = event.signal ? labelFor(event.agent, event.signal, event.confidence) : "Complete";
      setAgentStatus(ticker, event.agent, "done", resultLabel, event.duration_s, event.signal);
      if (event.summary) entry.agents[event.agent] = { signal: event.signal, confidence: event.confidence, summary: event.summary };
      entry.runningAgents?.delete(event.agent);
      // No live token stream for this agent? Its row can still peek at the
      // finished one-line summary while the rest of the run continues.
      const cell = $(`cell-${ticker}-${event.agent}`);
      if (cell && event.summary && !cell.classList.contains("has-stream") && !cell.classList.contains("has-peek")) {
        const row = cell.querySelector(".agent-row");
        const pane = $(`stream-${ticker}-${event.agent}`);
        const pre = $(`stream-pre-${ticker}-${event.agent}`);
        const label = AGENTS.find((agent) => agent.key === event.agent)?.label || event.agent;
        pre.textContent = event.summary;
        cell.classList.add("has-peek");
        pane.classList.add("peek");
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        row.setAttribute("aria-expanded", "false");
        row.setAttribute("aria-label", `${label} summary, press Enter to toggle`);
        const hint = row.querySelector(".stream-hint");
        if (hint) hint.hidden = false;
      }
      markAgentDone(entry, event.agent);
      updateProgress(ticker, entry);
      break;
    }
    case "agent_failed":
      entry.runningAgents?.delete(event.agent);
      setAgentStatus(ticker, event.agent, "failed", "⚠ Unavailable");
      markAgentDone(entry, event.agent);
      updateProgress(ticker, entry);
      break;
    case "ticker_failed":
      entry.failed = true;
      entry.done = entry.total;
      entry.runningAgents?.clear();
      setHeader(ticker, null, `failed: ${event.error || "market data unavailable"}`, null);
      updateProgress(ticker, entry);
      announceTickerResult(ticker, true);
      renderResultCard({ ticker, error: event.error || "Market data unavailable", decision: "HOLD", confidence: 0, risk_flags: [] });
      renderSummaryTable();
      break;
    case "ticker_completed":
      entry.analysis = event.analysis;
      entry.cached = Boolean(event.cached);
      entry.done = entry.total;
      entry.agents = {
        technical: event.analysis.technical, fundamental: event.analysis.fundamental,
        news: event.analysis.news, sentiment: event.analysis.sentiment, forecast: event.analysis.forecast,
        market: event.analysis.market,
        bull: event.analysis.bull, bear: event.analysis.bear, judge: event.analysis.judge,
        manager: { signal: event.decision, confidence: event.confidence, summary: event.analysis.summary },
      };
      updateProgress(ticker, entry);
      announceTickerResult(ticker, false, event.decision);
      renderResultCard(event.analysis);
      renderSummaryTable();
      break;
  }
}

function markAgentDone(entry, agent) {
  if (entry.completedAgents.has(agent)) return;
  entry.completedAgents.add(agent);
  entry.done = Math.min(entry.total, entry.done + 1);
}

// One announcement per ticker through the overall status region: the
// meaningful unit is the finished call, not each agent step (P0.3).
function announceTickerResult(ticker, failed, decision) {
  const status = $("overall-status");
  if (!status || !state.running) return;
  const total = state.tickers.size;
  const done = [...state.tickers.values()].filter((entry) => entry.done >= entry.total).length;
  status.textContent = failed
    ? `${ticker} failed · ${done} of ${total} analyzed`
    : `${ticker}: ${decision || "HOLD"} · ${done} of ${total} analyzed`;
}

export function updateProgress(ticker, entry) {
  const progress = $(`progress-${ticker}`);
  const count = $(`progc-${ticker}`);
  if (!progress || !count || !entry.total) return;
  const pct = Math.min(100, Math.round((entry.done / entry.total) * 100));
  progress.setAttribute("aria-valuenow", String(pct));
  progress.classList.toggle("finished", entry.done >= entry.total);
  progress.querySelectorAll(".seg").forEach((seg) => {
    const key = seg.dataset.agent;
    let stateClass = "";
    if (entry.completedAgents.has(key)) stateClass = entry.failed ? " failed" : " done";
    else if (entry.runningAgents?.has(key)) stateClass = " running";
    seg.className = `seg${stateClass}`;
  });
  count.textContent = entry.failed ? "Failed · retry available" : `${entry.done}/${entry.total}`;
  updateOverallProgress();
}

export function updateOverallProgress(forceComplete = false) {
  const entries = [...state.tickers.values()];
  const total = entries.reduce((sum, entry) => sum + entry.total, 0);
  const done = entries.reduce((sum, entry) => sum + entry.done, 0);
  const pct = forceComplete ? 100 : (total ? Math.min(100, Math.round((done / total) * 100)) : 0);
  $("overall-progress-fill").style.width = `${pct}%`;
  $("overall-progress").setAttribute("aria-valuenow", String(pct));
}
