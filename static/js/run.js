/* Run lifecycle: start, stream, cancel, finish, restore, and rerun. */

import { CLIENT_ID_KEY, LAST_RUN_KEY } from "./constants.js";
import { activeAgents, setDepth, setOutlook } from "./options.js";
import { state } from "./state.js";
import { addTickerTags, setTickerTags } from "./tickers.js";
import { handleEvent, updateOverallProgress, updateProgress } from "./events.js";
import { labelFor, renderProgressCard, renderResultCard, renderSummaryTable, setAgentStatus, setHeader } from "./render.js";
import { $, getClientId, hideError, showError, showToast } from "./util.js";

function storedRunId() {
  try { return localStorage.getItem(LAST_RUN_KEY); } catch (_) { return null; }
}

function persistRun(runId) {
  state.runId = runId;
  const url = new URL(window.location.href);
  url.searchParams.set("run", runId);
  window.history.replaceState({}, "", url);
  try { localStorage.setItem(LAST_RUN_KEY, runId); } catch (_) {}
}

function clearSavedRun(runId) {
  try {
    if (!runId || localStorage.getItem(LAST_RUN_KEY) === runId) localStorage.removeItem(LAST_RUN_KEY);
  } catch (_) {}
  const url = new URL(window.location.href);
  if (!runId || url.searchParams.get("run") === runId) {
    url.searchParams.delete("run");
    window.history.replaceState({}, "", url);
  }
}

export async function restoreSavedRun() {
  const urlRun = new URL(window.location.href).searchParams.get("run");
  const runId = urlRun || storedRunId();
  if (!runId) return;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    if (response.status === 404) {
      clearSavedRun(runId);
      if (urlRun) showRestoreNotice("That analysis is no longer on this server; start a new run when ready.");
      return;
    }
    if (!response.ok) throw new Error(`status ${response.status}`);
    const run = await response.json();
    // A finished run opens only when explicitly linked (?run=, e.g. from the
    // history page or a refresh right after completion). A fresh visit to /
    // starts clean; a still-running run always reconnects.
    if (run.status !== "running" && !urlRun) {
      clearSavedRun(runId);
      return;
    }
    persistRun(run.run_id);
    setOutlook(run.outlook);
    setDepth(run.depth);
    setTickerTags(run.tickers.join(","));
    beginRun(run.tickers, { startedAtMs: Number(run.started_at) * 1000, restoring: true });
    if (run.status === "running") {
      $("overall-status").textContent = "Restored active run · reconnecting";
      openStream(run.run_id);
      return;
    }
    hydrateResults(run.results || {}, true);
    finishRun({
      duration: run.duration_s,
      focusResults: false,
      failed: run.status === "failed",
      cancelled: run.status === "cancelled",
    });
    showRestoreNotice(
      run.status === "failed"
        ? "Restored an interrupted run; retry any ticker below."
        : run.status === "cancelled"
          ? "Restored a cancelled run; finished tickers are kept below. Run again reanalyzes everything."
          : "Restored from run history.",
    );
  } catch (_) {
    if (urlRun) showRestoreNotice("Could not restore that analysis; start a new run.");
  }
}

function showRestoreNotice(message) {
  const notice = $("restore-notice");
  notice.textContent = message;
  notice.classList.remove("hidden");
}

/* ---------- rerun with the same settings (ROADMAP P0.2) ---------- */

// The Runs page links here as /?rerun=1&tickers=...&outlook=...&depth=... to
// repeat a completed, failed, or cancelled run with its original settings.
export async function applyRerunParams() {
  const params = new URL(window.location.href).searchParams;
  if (!params.get("rerun")) return false;
  setOutlook(params.get("outlook"));
  setDepth(params.get("depth"));
  setTickerTags(params.get("tickers") || "");
  // Drop the params so a refresh starts clean instead of auto-running again.
  window.history.replaceState({}, "", "/");
  if (!state.tickerTags.length) {
    showRestoreNotice("That run could not be rerun; add tickers and analyze.");
    return true;
  }
  await startAnalysis();
  return true;
}

/* ---------- analysis ---------- */

export async function feelingLucky() {
  if (state.running || state.discovering) return;
  const button = $("feeling-lucky-btn");
  const originalLabel = button.textContent;
  state.discovering = true;
  button.disabled = true;
  $("analyze-btn").disabled = true;
  button.textContent = "Screening market…";
  hideError();
  try {
    const response = await fetch(`/api/discover?outlook=${encodeURIComponent(state.outlook)}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `screen failed (${response.status})`);
    setTickerTags(payload.tickers.join(","));
    showToast(`Picked ${payload.tickers.join(", ")} from ${payload.universe_size} candidates, analyzing…`);
    state.discovering = false;
    await startAnalysis();
  } catch (error) {
    showError(`Could not select candidates: ${error.message}`);
  } finally {
    state.discovering = false;
    button.textContent = originalLabel;
    if (!state.running) {
      button.disabled = false;
      $("analyze-btn").disabled = state.tickerTags.length === 0;
    }
  }
}

export async function startAnalysis() {
  if (state.running || state.discovering) return;
  addTickerTags($("ticker-input").value); // pick up a half-typed symbol too
  if (!state.tickerTags.length) {
    showError("Add at least one ticker first (e.g. NVDA).");
    $("ticker-input").focus();
    return;
  }

  hideError();
  $("restore-notice").classList.add("hidden");
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tickers: state.tickerTags,
        outlook: state.outlook,
        depth: state.depth,
        client_id: getClientId(CLIENT_ID_KEY),
      }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || `Analysis failed (${response.status})`);
      return;
    }
    const payload = await response.json();
    persistRun(payload.run_id);
    beginRun(state.tickerTags);
    openStream(payload.run_id);
  } catch (error) {
    showError(`Could not reach the server: ${error.message}`);
  }
}

export function beginRun(tickers, options = {}) {
  if (state.es) state.es.close();
  state.es = null;
  state.running = true;
  state.finishing = false;
  state.streamWarningShown = false;
  const agents = activeAgents();
  state.tickers = new Map(tickers.map((ticker) => [ticker, {
    agents: {}, completedAgents: new Set(), done: 0, total: agents.length, failed: false,
  }]));
  state.runStartedAtMs = options.startedAtMs || Date.now();
  state.chats = new Map();
  $("analyze-btn").disabled = true;
  $("feeling-lucky-btn").disabled = true;
  $("how-section").classList.add("hidden");
  $("results-section").classList.add("hidden");
  $("analyze-another-btn").classList.add("hidden");
  $("run-again-btn").classList.add("hidden");
  $("cancel-note").classList.add("hidden");
  const cancelBtn = $("cancel-btn");
  cancelBtn.classList.remove("hidden");
  cancelBtn.disabled = false;
  $("results-list").innerHTML = "";
  $("summary-panel").innerHTML = "";
  $("live-section").classList.remove("hidden");
  $("live-grid").innerHTML = "";
  $("overall-status").textContent = options.restoring ? "Restoring analysis" : "Starting research";
  for (const ticker of tickers) renderProgressCard(ticker);
  updateOverallProgress();
  window.clearInterval(state.timer);
  updateRunTimer();
  state.timer = window.setInterval(updateRunTimer, 1000);
}

function updateRunTimer() {
  const seconds = Math.max(0, (Date.now() - state.runStartedAtMs) / 1000).toFixed(0);
  $("run-timer").textContent = `· ${seconds}s`;
}

export function openStream(runId) {
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  state.es = source;
  source.onopen = () => {
    if (state.running) $("overall-status").textContent = "Analysis in progress";
    state.streamWarningShown = false;
  };
  source.onmessage = async (message) => {
    const event = JSON.parse(message.data);
    handleEvent(event);
    if (event.type === "analysis_completed" && !state.finishing) {
      state.finishing = true;
      source.close();
      state.es = null;
      await syncRunResults(runId);
      const failed = event.status === "failed";
      const cancelled = event.status === "cancelled";
      finishRun({ duration: event.duration_s, focusResults: true, failed, cancelled });
      if (failed) showToast(event.error || "The run stopped before it could complete.", true);
      else if (cancelled) showToast("Run cancelled · finished tickers are preserved below.");
    }
  };
  source.onerror = () => {
    if (!state.running) return;
    $("overall-status").textContent = "Connection interrupted · reconnecting";
    if (!state.streamWarningShown) {
      showToast("Live connection interrupted. Reconnecting automatically.", true);
      state.streamWarningShown = true;
    }
  };
}

async function syncRunResults(runId) {
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    if (!response.ok) return;
    const run = await response.json();
    hydrateResults(run.results || {}, false);
  } catch (_) {}
}

function hydrateResults(results, restored) {
  Object.entries(results).forEach(([ticker, analysis]) => {
    const entry = state.tickers.get(ticker);
    if (!entry) return;
    entry.analysis = analysis;
    entry.done = entry.total;
    entry.failed = Boolean(analysis.error);
    setHeader(ticker, analysis.price, analysis.company_name, analysis.providers);
    activeAgents().forEach((agent) => {
      const result = analysis[agent.key];
      // Runs saved before an agent existed have no record of it: show
      // "Not recorded" instead of "Unavailable". Old rows serialize the
      // forecast agent as null with no forecast_method; a null agent on a
      // run that has forecast_method genuinely failed.
      const legacyMissing = agent.rebuttal
        ? !(agent.key in analysis)
        : (agent.key === "forecast" && !result && !analysis.forecast_method)
          || (agent.key === "sentiment" && !result && analysis.providers && !("social" in analysis.providers));
      if (agent.rebuttal && legacyMissing && !restored) return;
      const available = agent.key === "manager" ? !analysis.error : Boolean(result);
      const statusClass = legacyMissing ? "neutral" : available ? "done" : "failed";
      const resultLabel = agent.key === "manager"
        ? analysis.decision
        : labelFor(agent.key, result?.signal, result?.confidence);
      const statusText = legacyMissing
        ? "Not recorded"
        : available
          ? `✓ ${resultLabel === "n/a" ? "Complete" : resultLabel}`
          : "⚠ Unavailable";
      setAgentStatus(ticker, agent.key, statusClass, statusText);
      entry.completedAgents.add(agent.key);
    });
    updateProgress(ticker, entry);
    renderResultCard(analysis);
  });
  renderSummaryTable();
  if (restored && Object.keys(results).length) $("results-section").classList.remove("hidden");
}

export function finishRun({ duration = null, focusResults = true, failed = false, cancelled = false } = {}) {
  state.running = false;
  state.finishing = false;
  $("analyze-btn").disabled = state.tickerTags.length === 0;
  $("feeling-lucky-btn").disabled = false;
  $("cancel-btn").classList.add("hidden");
  window.clearInterval(state.timer);
  state.timer = null;
  const elapsed = duration == null ? Math.max(0, (Date.now() - state.runStartedAtMs) / 1000) : Number(duration);
  const endWord = failed ? "stopped" : cancelled ? "cancelled" : "done";
  $("run-timer").textContent = `· ${endWord} in ${elapsed.toFixed(1)}s`;
  $("overall-status").textContent = failed || cancelled
    ? "Partial results preserved"
    : "Analysis complete";
  if (cancelled) {
    $("cancel-note").classList.remove("hidden");
    markInterruptedTickers();
  }
  updateOverallProgress(true);
  if ($("results-list").children.length) {
    $("results-section").classList.remove("hidden");
    $("analyze-another-btn").classList.remove("hidden");
    $("run-again-btn").classList.remove("hidden");
    if (focusResults) $("results-heading").focus({ preventScroll: false });
  }
}

/* ---------- run cancellation (ROADMAP P0.2) ---------- */

export async function cancelRun() {
  if (!state.running || !state.runId) return;
  const button = $("cancel-btn");
  button.disabled = true;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/cancel`, { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `failed (${response.status})`);
    // The terminal SSE event may have landed while the request was in flight;
    // settle only if the stream did not already finish the run.
    if (payload.status === "cancelled") settleCancelled();
    else finishRun({ failed: payload.status === "failed", cancelled: false, focusResults: true });
  } catch (error) {
    button.disabled = false;
    showError(`Could not cancel the run: ${error.message}`);
  }
}

function settleCancelled() {
  if (state.finishing || !state.running) return;
  state.finishing = true;
  if (state.es) { state.es.close(); state.es = null; }
  syncRunResults(state.runId).finally(() => finishRun({ focusResults: true, cancelled: true }));
}

// Tickers still mid-analysis when a run ends by cancellation have no result;
// label their rows so the progress grid never shows eternal "Running…" cells.
function markInterruptedTickers() {
  for (const [ticker, entry] of state.tickers) {
    if (entry.analysis) continue;
    const count = $(`progc-${ticker}`);
    if (count) count.textContent = "Cancelled";
    for (const agent of activeAgents()) {
      if (entry.completedAgents.has(agent.key)) continue;
      setAgentStatus(ticker, agent.key, "neutral", "Cancelled");
    }
  }
}
