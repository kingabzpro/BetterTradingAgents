/* BetterTradingAgents - decision cockpit and resilient run recovery. */

const ICONS = {
  technical: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12.5 6 8l2.5 2.5L13.5 4"/></svg>',
  fundamental: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M3 3.5h10M3 7h10M3 10.5h6"/><circle cx="12.4" cy="10.7" r="1.6"/></svg>',
  news: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="5.7"/><path d="M2.3 8h11.4M8 2.3c-1.8 1.6-2.7 3.5-2.7 5.7s.9 4.1 2.7 5.7c1.8-1.6 2.7-3.5 2.7-5.7S9.8 3.9 8 2.3z"/></svg>',
  forecast: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12.5 6 9l2.5 2.5L12 8"/><path d="M12 8l2.3-2.3" stroke-dasharray="1.5 1.3"/><path d="M12.2 5.7h2.1v2.1"/></svg>',
  bull: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 12.5 12.5 3.5M6.5 3.5h6v6"/></svg>',
  bear: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3.5l9 9M12.5 6.5v6h-6"/></svg>',
  bull_rebuttal: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 12.5 12.5 3.5M6.5 3.5h6v6"/><path d="M2.5 5.5h3M2.5 8h2"/></svg>',
  bear_rebuttal: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3.5l9 9M12.5 6.5v6h-6"/><path d="M2.5 5.5h3M2.5 8h2"/></svg>',
  manager: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5" width="11" height="8" rx="2"/><path d="M6 5V3.6A1.6 1.6 0 0 1 7.6 2h.8A1.6 1.6 0 0 1 10 3.6V5M2.5 8.5h11"/></svg>',
};

const AGENTS = [
  { key: "technical", label: "Technical", stage: "Research" },
  { key: "fundamental", label: "Fundamentals", stage: "Research" },
  { key: "news", label: "News", stage: "Research" },
  { key: "forecast", label: "Forecast", stage: "Research" },
  { key: "bull", label: "Bull", stage: "Debate" },
  { key: "bear", label: "Bear", stage: "Debate" },
  { key: "bull_rebuttal", label: "Bull rebuttal", stage: "Debate", rebuttal: true },
  { key: "bear_rebuttal", label: "Bear rebuttal", stage: "Debate", rebuttal: true },
  { key: "manager", label: "Portfolio manager", stage: "Decision" },
];

const LAST_RUN_KEY = "bta:lastRunId";
const CLIENT_ID_KEY = "bta:clientId";
const TICKER_PATTERN = /^[A-Z0-9.\-]{1,10}$/;
const OUTLOOKS = ["day_trade", "short_term", "long_term"];
const OUTLOOK_LABELS = { day_trade: "Day trading", short_term: "Short term", long_term: "Long term" };
const ADVANCED_OPEN_KEY = "bta:advancedOpen";
const OUTLOOK_KEY = "bta:outlook";
const DEPTH_KEY = "bta:depth";
const DEPTH_PROFILES = {
  fast: { label: "Fast", research: ["technical", "news"], rebuttals: false },
  medium: { label: "Medium", research: ["technical", "fundamental", "news", "forecast"], rebuttals: false },
  expert: { label: "Expert", research: ["technical", "fundamental", "news", "forecast"], rebuttals: true },
};
const EVIDENCE_META = {
  technical: { title: "Technical", help: "Trend, momentum, volatility and volume" },
  fundamental: { title: "Fundamentals", help: "Growth, margins and valuation" },
  news: { title: "News", help: "Recent potentially market-moving headlines" },
  forecast: { title: "Forecast", help: "5-day statistical projection vs volatility" },
};
const $ = (id) => document.getElementById(id);
const state = {
  running: false,
  discovering: false,
  finishing: false,
  es: null,
  runId: null,
  tickers: new Map(),
  runStartedAtMs: null,
  timer: null,
  debateRounds: 1,
  streamWarningShown: false,
  tickerTags: [],
  maxTickers: 5,
  outlook: "short_term",
  depth: "medium",
};

/* ---------- boot and recovery ---------- */

document.addEventListener("DOMContentLoaded", async () => {
  $("analyze-btn").addEventListener("click", () => startAnalysis());
  $("feeling-lucky-btn").addEventListener("click", () => feelingLucky());
  $("analyze-another-btn").addEventListener("click", () => analyzeAnother());
  $("add-ticker-btn").addEventListener("click", () => addTickerTags($("ticker-input").value));
  $("ticker-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      if ($("ticker-input").value.trim()) addTickerTags($("ticker-input").value);
      else startAnalysis();
    }
    if (event.key === "Backspace" && !$("ticker-input").value && state.tickerTags.length) {
      removeTickerTag(state.tickerTags[state.tickerTags.length - 1]);
    }
  });
  document.querySelectorAll(".chip-btn").forEach((chip) => {
    chip.addEventListener("click", () => addTickerTags(chip.dataset.tickers));
  });
  document.querySelectorAll("[data-outlook]").forEach((button) => {
    button.addEventListener("click", () => setOutlook(button.dataset.outlook));
  });
  document.querySelectorAll("[data-depth]").forEach((button) => {
    button.addEventListener("click", () => setDepth(button.dataset.depth));
  });
  $("advanced-options").addEventListener("toggle", () => {
    try { localStorage.setItem(ADVANCED_OPEN_KEY, $("advanced-options").open ? "1" : "0"); } catch (_) {}
  });
  try { if (localStorage.getItem(ADVANCED_OPEN_KEY) === "1") $("advanced-options").open = true; } catch (_) {}
  try {
    setOutlook(localStorage.getItem(OUTLOOK_KEY));
    setDepth(localStorage.getItem(DEPTH_KEY));
  } catch (_) {}
  updateAdvancedSummary();
  renderTickerTags();

  try {
    const response = await fetch("/api/health");
    const health = await response.json();
    if (health.mock_mode) $("mode-chip").classList.remove("hidden");
    state.debateRounds = health.debate_rounds || 1;
    state.maxTickers = health.max_tickers || 5;
    updateDepthLabels();
    const providers = health.providers || {};
    $("provider-line").textContent =
      `data: ${providers.prices || "?"} prices, ${providers.fundamentals || "?"} fundamentals, ` +
      `${providers.news_search || "?"} news search, ${providers.forecast || "local"} forecast. model: ${health.llm_model || "mock"}`;
  } catch (_) {
    // The analysis request will show a concrete error if the server is unavailable.
  }
  await restoreSavedRun();
});

/* ---------- ticker tag input ---------- */

function addTickerTags(raw) {
  const parts = String(raw || "")
    .split(/[\s,]+/)
    .map((ticker) => ticker.trim().toUpperCase())
    .filter(Boolean);
  if (!parts.length) return;
  const invalid = parts.filter((ticker) => !TICKER_PATTERN.test(ticker));
  if (invalid.length) {
    showError(`That does not look like a ticker symbol: ${invalid.join(", ")}.`);
    return;
  }
  const next = [...state.tickerTags];
  for (const ticker of parts) {
    if (!next.includes(ticker)) next.push(ticker);
  }
  if (next.length > state.maxTickers) {
    showError(`Max ${state.maxTickers} tickers at once. Remove one before adding more.`);
    return;
  }
  hideError();
  state.tickerTags = next;
  $("ticker-input").value = "";
  renderTickerTags();
}

function removeTickerTag(ticker) {
  state.tickerTags = state.tickerTags.filter((existing) => existing !== ticker);
  renderTickerTags();
}

function setTickerTags(raw) {
  state.tickerTags = String(raw || "")
    .split(/[\s,]+/)
    .map((ticker) => ticker.trim().toUpperCase())
    .filter((ticker) => TICKER_PATTERN.test(ticker))
    .slice(0, state.maxTickers);
  renderTickerTags();
}

function renderTickerTags() {
  $("ticker-tags").innerHTML = state.tickerTags.map((ticker) => `
    <span class="ticker-tag">${escapeHtml(ticker)}<button type="button" class="tag-remove" data-remove="${escapeAttr(ticker)}" aria-label="Remove ${escapeAttr(ticker)}">×</button></span>`
  ).join("");
  $("ticker-tags").querySelectorAll("[data-remove]").forEach((button) => {
    button.addEventListener("click", () => removeTickerTag(button.dataset.remove));
  });
  $("add-ticker-btn").disabled = state.tickerTags.length >= state.maxTickers;
  if (!state.running && !state.discovering) $("analyze-btn").disabled = state.tickerTags.length === 0;
}

/* ---------- outlook + depth selectors ---------- */

function setOutlook(outlook) {
  state.outlook = OUTLOOKS.includes(outlook) ? outlook : "short_term";
  document.querySelectorAll("[data-outlook]").forEach((button) => {
    const selected = button.dataset.outlook === state.outlook;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-checked", String(selected));
  });
  try { localStorage.setItem(OUTLOOK_KEY, state.outlook); } catch (_) {}
  updateAdvancedSummary();
}

function setDepth(depth) {
  state.depth = DEPTH_PROFILES[depth] ? depth : "medium";
  document.querySelectorAll("[data-depth]").forEach((button) => {
    const selected = button.dataset.depth === state.depth;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-checked", String(selected));
  });
  try { localStorage.setItem(DEPTH_KEY, state.depth); } catch (_) {}
  updateAdvancedSummary();
}

// The collapsed disclosure still shows the current selections.
function updateAdvancedSummary() {
  const values = $("adv-values");
  if (values) values.textContent = `${OUTLOOK_LABELS[state.outlook]} · ${depthProfile().label}`;
}

function depthProfile() {
  return DEPTH_PROFILES[state.depth] || DEPTH_PROFILES.medium;
}

function agentCount(profileKey) {
  const profile = DEPTH_PROFILES[profileKey];
  return profile.research.length + 3 + (profile.rebuttals && state.debateRounds >= 2 ? 2 : 0);
}

// Agent counts on the buttons depend on the server's debate-rounds setting.
function updateDepthLabels() {
  const labels = {
    fast: `Tech + news · ${agentCount("fast")} agents`,
    medium: `All research · ${agentCount("medium")} agents`,
    expert: `Full debate · ${agentCount("expert")} agents`,
  };
  document.querySelectorAll("[data-depth]").forEach((button) => {
    const small = button.querySelector("small");
    if (small && labels[button.dataset.depth]) small.textContent = labels[button.dataset.depth];
  });
}

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

async function restoreSavedRun() {
  const urlRun = new URL(window.location.href).searchParams.get("run");
  const runId = urlRun || storedRunId();
  if (!runId) return;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    if (response.status === 404) {
      clearSavedRun(runId);
      if (urlRun) showRestoreNotice("That analysis is no longer available on this server. Start a new run when you are ready.");
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
    beginRun(run.tickers, { startedAtMs: Number(run.started_at) * 1000, restoring: true });
    if (run.status === "running") {
      $("overall-status").textContent = "Restored active run · reconnecting";
      openStream(run.run_id);
      return;
    }
    hydrateResults(run.results || {}, true);
    finishRun({ duration: run.duration_s, focusResults: false, failed: run.status === "failed" });
    showRestoreNotice(run.status === "failed" ? "Restored an interrupted analysis. You can retry any ticker below." : "Restored analysis from run history.");
  } catch (_) {
    if (urlRun) showRestoreNotice("We could not restore that analysis. You can start a new run.");
  }
}

function showRestoreNotice(message) {
  const notice = $("restore-notice");
  notice.textContent = message;
  notice.classList.remove("hidden");
}

/* ---------- analysis ---------- */

async function feelingLucky() {
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
    showToast(`Selected ${payload.tickers.join(", ")} from ${payload.universe_size} emerging growth stocks. Starting full analysis…`);
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

async function startAnalysis() {
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
        client_id: getClientId(),
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

function beginRun(tickers, options = {}) {
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
  $("analyze-btn").disabled = true;
  $("feeling-lucky-btn").disabled = true;
  $("how-section").classList.add("hidden");
  $("results-section").classList.add("hidden");
  $("analyze-another-btn").classList.add("hidden");
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

function activeAgents() {
  const profile = depthProfile();
  return AGENTS.filter((agent) => {
    if (agent.stage === "Research") return profile.research.includes(agent.key);
    if (agent.rebuttal) return profile.rebuttals && state.debateRounds >= 2;
    return true;
  });
}

function updateRunTimer() {
  const seconds = Math.max(0, (Date.now() - state.runStartedAtMs) / 1000).toFixed(0);
  $("run-timer").textContent = `· ${seconds}s`;
}

function openStream(runId) {
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
      finishRun({ duration: event.duration_s, focusResults: true, failed });
      if (failed) showToast(event.error || "The run stopped before it could complete.", true);
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
        : agent.key === "forecast" && !result && !analysis.forecast_method;
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

function finishRun({ duration = null, focusResults = true, failed = false } = {}) {
  state.running = false;
  state.finishing = false;
  $("analyze-btn").disabled = state.tickerTags.length === 0;
  $("feeling-lucky-btn").disabled = false;
  window.clearInterval(state.timer);
  state.timer = null;
  const elapsed = duration == null ? Math.max(0, (Date.now() - state.runStartedAtMs) / 1000) : Number(duration);
  $("run-timer").textContent = `· ${failed ? "stopped" : "done"} in ${elapsed.toFixed(1)}s`;
  $("overall-status").textContent = failed ? "Run stopped · partial results preserved" : "Analysis complete";
  updateOverallProgress(true);
  if ($("results-list").children.length) {
    $("results-section").classList.remove("hidden");
    $("analyze-another-btn").classList.remove("hidden");
    if (focusResults) $("results-heading").focus({ preventScroll: false });
  }
}

function analyzeAnother(prefill = "") {
  if (state.running) return;
  if (prefill) setTickerTags(prefill);
  $("ticker-input").focus();
  $("ticker-input").scrollIntoView({ behavior: "smooth", block: "center" });
}

function retryTicker(ticker) {
  analyzeAnother(ticker);
  showToast(`${ticker} is ready to retry. Press Analyze Stocks when you are ready.`);
}

/* ---------- SSE events -> progress UI ---------- */

function handleEvent(event) {
  const { ticker } = event;
  if (!ticker || !state.tickers.has(ticker)) return;
  const entry = state.tickers.get(ticker);
  switch (event.type) {
    case "ticker_data":
      setHeader(ticker, event.price, event.company_name, event.sources);
      break;
    case "agent_started":
      setAgentStatus(ticker, event.agent, "running", "Running…");
      break;
    case "agent_completed": {
      const resultLabel = event.signal ? labelFor(event.agent, event.signal, event.confidence) : "Complete";
      setAgentStatus(ticker, event.agent, "done", `✓ ${resultLabel}`, event.duration_s);
      if (event.summary) entry.agents[event.agent] = { signal: event.signal, confidence: event.confidence, summary: event.summary };
      markAgentDone(entry, event.agent);
      updateProgress(ticker, entry);
      break;
    }
    case "agent_failed":
      setAgentStatus(ticker, event.agent, "failed", "⚠ Unavailable");
      markAgentDone(entry, event.agent);
      updateProgress(ticker, entry);
      break;
    case "ticker_failed":
      entry.failed = true;
      entry.done = entry.total;
      setHeader(ticker, null, `failed: ${event.error || "market data unavailable"}`, null);
      updateProgress(ticker, entry);
      renderResultCard({ ticker, error: event.error || "Market data unavailable", decision: "HOLD", confidence: 0, risk_flags: [] });
      renderSummaryTable();
      break;
    case "ticker_completed":
      entry.analysis = event.analysis;
      entry.done = entry.total;
      entry.agents = {
        technical: event.analysis.technical, fundamental: event.analysis.fundamental,
        news: event.analysis.news, forecast: event.analysis.forecast,
        bull: event.analysis.bull, bear: event.analysis.bear,
        bull_rebuttal: event.analysis.bull_rebuttal, bear_rebuttal: event.analysis.bear_rebuttal,
        manager: { signal: event.decision, confidence: event.confidence, summary: event.analysis.summary },
      };
      updateProgress(ticker, entry);
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

function updateProgress(ticker, entry) {
  const progress = $(`progress-${ticker}`);
  const fill = $(`prog-${ticker}`);
  const count = $(`progc-${ticker}`);
  if (!progress || !fill || !count || !entry.total) return;
  const pct = Math.min(100, Math.round((entry.done / entry.total) * 100));
  fill.style.width = `${pct}%`;
  progress.setAttribute("aria-valuenow", String(pct));
  count.textContent = entry.failed ? "Failed · retry available" : `${entry.done}/${entry.total}`;
  updateOverallProgress();
}

function updateOverallProgress(forceComplete = false) {
  const entries = [...state.tickers.values()];
  const total = entries.reduce((sum, entry) => sum + entry.total, 0);
  const done = entries.reduce((sum, entry) => sum + entry.done, 0);
  const pct = forceComplete ? 100 : (total ? Math.min(100, Math.round((done / total) * 100)) : 0);
  $("overall-progress-fill").style.width = `${pct}%`;
  $("overall-progress").setAttribute("aria-valuenow", String(pct));
}

function labelFor(agent, signal, confidence) {
  if (agent === "bull" || agent === "bear" || agent.endsWith("_rebuttal")) return `${Math.round((confidence ?? 0) * 100)}%`;
  const map = { bullish: "Bullish", bearish: "Bearish", neutral: "Neutral", positive: "Positive", negative: "Negative", unknown: "n/a" };
  return map[signal] || signal || "n/a";
}

/* ---------- rendering ---------- */

function convictionLabel(confidence) {
  const pct = Math.round((confidence || 0) * 100);
  if (pct >= 70) return "High evidence";
  if (pct >= 50) return "Moderate evidence";
  return "Low evidence";
}

function decisionMeta(decision) {
  return { BUY: { cls: "buy", icon: "▲" }, HOLD: { cls: "hold", icon: "■" }, SELL: { cls: "sell", icon: "▼" } }[decision] || { cls: "hold", icon: "■" };
}

function decisionBadge(analysis) {
  const meta = decisionMeta(analysis.decision);
  return `<span class="decision ${meta.cls}"><span class="d-icon" aria-hidden="true">${meta.icon}</span>${escapeHtml(analysis.decision || "HOLD")}</span>`;
}

function renderSummaryTable() {
  const analyses = [...state.tickers.values()].map((entry) => entry.analysis).filter(Boolean);
  const panel = $("summary-panel");
  panel.innerHTML = "";
  if (!analyses.length) return;
  const table = document.createElement("table");
  table.id = "summary-table";
  table.innerHTML = `
    <caption class="sr-only">Decision summary for analyzed tickers</caption>
    <thead><tr><th scope="col">Ticker</th><th scope="col">Decision</th><th scope="col" class="num">5-day forecast</th><th scope="col" class="num">Evidence</th><th scope="col" class="num">Suggested size</th><th scope="col">Risk</th><th scope="col"><span class="sr-only">Action</span></th></tr></thead>
    <tbody>${analyses.map((analysis) => {
      const flags = analysis.risk_flags || [];
      return `<tr>
        <td data-label="Ticker"><strong>${escapeHtml(analysis.ticker)}</strong></td>
        <td data-label="Decision">${decisionBadge(analysis)}</td>
        <td data-label="5-day forecast" class="num">${analysis.forecast_price_5d != null ? `$${Number(analysis.forecast_price_5d).toFixed(2)} (${Number(analysis.forecast_change_5d_pct) >= 0 ? "+" : ""}${Number(analysis.forecast_change_5d_pct).toFixed(2)}%)` : "Not available"}<small>${analysis.forecast_method === "timegpt-1" ? "Nixtla TimeGPT" : "Local model"}</small></td>
        <td data-label="Evidence" class="num">${Math.round((analysis.confidence || 0) * 100)}% · ${convictionLabel(analysis.confidence)}</td>
        <td data-label="Suggested size" class="num">${analysis.suggested_size_usd ? fmtUsd(analysis.suggested_size_usd) : "Not available"}</td>
        <td data-label="Risk">${analysis.error ? '<span class="flag-warn">⚠ Unavailable</span>' : flags.length ? `<span class="flag-warn">⚠ ${flags.length} flag${flags.length > 1 ? "s" : ""}</span>` : '<span class="flag-ok">✓ Clear</span>'}</td>
        <td data-label="Action"><button class="table-open-btn" type="button" data-open-ticker="${escapeAttr(analysis.ticker)}">View evidence</button></td>
      </tr>`;
    }).join("")}</tbody>`;
  $("summary-panel").appendChild(table);
  $("summary-panel").insertAdjacentHTML("beforeend", '<p class="results-help"><strong>Confidence means evidence strength, not probability of profit.</strong> Suggested size is a simulated risk-layer output. Every call remains educational, not investment advice.</p>');
  $("summary-panel").querySelectorAll("[data-open-ticker]").forEach((button) => button.addEventListener("click", () => openResult(button.dataset.openTicker)));
}

function renderProgressCard(ticker) {
  const card = document.createElement("article");
  card.className = "ticker-card";
  card.id = `live-${ticker}`;
  card.setAttribute("aria-labelledby", `live-title-${ticker}`);
  const agents = activeAgents();
  card.innerHTML = `
    <div class="ticker-head"><span class="tk" id="live-title-${ticker}">${escapeHtml(ticker)} <span class="px" id="px-${ticker}">fetching data…</span></span><span class="src" id="src-${ticker}"></span><span class="muted prog-count" id="progc-${ticker}">0/${agents.length}</span></div>
    <div class="run-progress" id="progress-${ticker}" role="progressbar" aria-label="${escapeAttr(ticker)} analysis progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="run-progress-fill" id="prog-${ticker}"></div></div>
    ${agents.map((agent) => `<div class="agent-row ${agent.stage !== "Research" ? "stage2" : ""}"><span class="agent-left"><span class="agent-icon">${ICONS[agent.key]}</span><span>${agent.label}<small>${agent.stage}</small></span></span><span class="status" id="status-${ticker}-${agent.key}" role="status" aria-live="polite"><span class="icon" aria-hidden="true"></span>Waiting</span></div>`).join("")}`;
  $("live-grid").appendChild(card);
}

function setHeader(ticker, price, name, sources) {
  const priceElement = $(`px-${ticker}`);
  const sourceElement = $(`src-${ticker}`);
  if (priceElement && price != null) priceElement.textContent = `$${Number(price).toFixed(2)}`;
  else if (priceElement && name) priceElement.textContent = name;
  if (sourceElement && sources) {
    const forecastSource = sources.forecast === "timegpt" ? "Nixtla TimeGPT" : sources.forecast === "local" ? "local forecast" : null;
    const unique = [...new Set([sources.prices, sources.fundamentals, sources.news, forecastSource].filter((source) => source && source !== "none"))];
    sourceElement.textContent = unique.length ? `via ${unique.join(" + ")}` : "";
  }
}

function setAgentStatus(ticker, agent, statusClass, text, duration) {
  const element = $(`status-${ticker}-${agent}`);
  if (!element) return;
  element.className = `status ${statusClass}`;
  const durationText = duration ? `${Number(duration).toFixed(1)}s` : "";
  element.innerHTML = `<span class="icon" aria-hidden="true"></span><span class="status-label">${escapeHtml(text)}</span>${durationText ? `<span class="status-duration">${durationText}</span>` : ""}`;
  element.setAttribute("aria-label", `${text}${durationText ? `, ${durationText}` : ""}`);
}

function signalClass(signal) { return `sig-${String(signal || "unknown").toLowerCase()}`; }

function plainEnglish(analysis) {
  const strength = convictionLabel(analysis.confidence).toLowerCase();
  if (analysis.error) return "Analysis failed, so there is no reliable call for this ticker yet.";
  const downgraded = (analysis.risk_flags || []).some((flag) => flag.toLowerCase().includes("downgraded"));
  if (analysis.decision === "BUY") return analysis.suggested_size_usd ? `The bull case won with ${strength}. For this simulation, the risk layer suggests ${fmtUsd(analysis.suggested_size_usd)}.` : `The bull case won with ${strength}, but the risk layer did not size a position.`;
  if (analysis.decision === "SELL") return `The bear case won with ${strength}. The agents suggest exiting or staying away.`;
  return downgraded ? "The evidence leaned positive, but a risk rule held the trade back. Review the flags before acting." : `The evidence is mixed with ${strength}. The agents suggest standing pat.`;
}

function evidenceCard(title, description, result, agent) {
  const signal = result?.signal || "unknown";
  return `<article class="evidence-card"><div class="mc-title">${escapeHtml(title)}</div><p class="mc-help">${escapeHtml(description)}</p><div class="mc-sig ${signalClass(signal)}">${escapeHtml(labelFor(agent, signal, result?.confidence))}</div><p class="mc-sum">${escapeHtml(result?.summary || "No reliable evidence was returned for this section.")}</p></article>`;
}

function providerText(providers) {
  const values = Object.entries(providers || {}).filter(([, value]) => value && value !== "none");
  return values.length ? values.map(([kind, provider]) => `${kind}: ${provider}`).join(" · ") : "Provider metadata unavailable";
}

function renderSources(references) {
  const sources = (references || []).filter((source) => source && source.title);
  if (!sources.length) return '<p class="empty-sources">No linked news sources were available for this run.</p>';
  return `<ul class="source-list">${sources.map((source) => {
    const url = safeUrl(source.url);
    const title = escapeHtml(source.title);
    const titleMarkup = url ? `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${title}<span aria-hidden="true"> ↗</span><span class="sr-only"> (opens in a new tab)</span></a>` : `<span>${title}</span>`;
    return `<li>${titleMarkup}<div>${escapeHtml(source.provider || "Unknown publisher")}${source.published_at ? ` · ${escapeHtml(formatDate(source.published_at))}` : ""}</div></li>`;
  }).join("")}</ul>`;
}

function renderResultCard(analysis) {
  $("results-section").classList.remove("hidden");
  const ticker = analysis.ticker;
  const card = document.createElement("article");
  card.className = `result-card${analysis.error ? " result-error" : ""}`;
  card.id = `result-${ticker}`;
  const detailId = `result-detail-${ticker}`;
  const confidencePct = Math.round((analysis.confidence || 0) * 100);
  const flags = analysis.risk_flags || [];
  const forecastResult = analysis.forecast
    || (analysis.forecast_method
      ? null
      : { signal: "unknown", confidence: 0, summary: "This run predates the Forecast analyst, so no projection was recorded." });
  const canAdd = analysis.decision === "BUY" && analysis.price && !analysis.error;
  const positionSize = analysis.suggested_size_usd || 10000;
  const defaultQty = analysis.price ? Math.max(1, Math.floor(positionSize / analysis.price)) : 0;
  const asOf = analysis.as_of ? formatDateTime(analysis.as_of) : "Timestamp unavailable";
  const profile = depthProfile();
  const skippedResearch = Object.keys(EVIDENCE_META).filter((key) => !profile.research.includes(key));
  const evidenceHtml = profile.research.map((key) => evidenceCard(
    EVIDENCE_META[key].title,
    EVIDENCE_META[key].help,
    key === "forecast" ? forecastResult : analysis[key],
    key,
  )).join("");

  card.innerHTML = `
    <button class="result-summary" type="button" aria-expanded="false" aria-controls="${detailId}"><span class="result-identity"><span class="tk">${escapeHtml(ticker)}</span><span class="company">${escapeHtml(analysis.company_name || "Company name unavailable")}</span></span>${decisionBadge(analysis)}<span class="summary-action">Evidence &amp; sources <span class="caret" aria-hidden="true">▶</span></span></button>
    <div class="decision-brief">
      <div class="decision-facts"><div><span>Current price</span><strong>${analysis.price != null ? `$${Number(analysis.price).toFixed(2)}` : "Unavailable"}</strong></div><div><span>5-day forecast</span><strong>${analysis.forecast_price_5d != null ? `$${Number(analysis.forecast_price_5d).toFixed(2)} (${Number(analysis.forecast_change_5d_pct) >= 0 ? "+" : ""}${Number(analysis.forecast_change_5d_pct).toFixed(2)}%)` : "Unavailable"}</strong><small>${analysis.forecast_method === "timegpt-1" ? "Nixtla TimeGPT; not a price guarantee." : analysis.forecast_trend_r2 != null ? `Local trend fit R² ${Number(analysis.forecast_trend_r2).toFixed(2)}; not a price guarantee.` : "Local historical projection; not a price guarantee."}</small></div><div><span>Data as of</span><strong>${escapeHtml(asOf)}</strong></div><div><span>Confidence</span><strong>${confidencePct}% · ${convictionLabel(analysis.confidence)}</strong><small>Evidence strength, not profit probability.</small></div><div><span>Suggested size</span><strong>${analysis.suggested_size_usd ? fmtUsd(analysis.suggested_size_usd) : "No position"}</strong><small>Simulated risk-layer output.</small></div></div>
      <div class="manager-conclusion"><span class="eyebrow">Manager conclusion</span><p class="plain-english">${escapeHtml(plainEnglish(analysis))}</p><p class="thesis">${escapeHtml(analysis.summary || analysis.error || "No manager summary was returned.")}</p></div>
      ${analysis.error ? `<div class="risk-flags"><strong>Analysis unavailable</strong><span>⚠ ${escapeHtml(analysis.error)}</span></div>` : flags.length ? `<div class="risk-flags"><strong>Risk flags</strong>${flags.map((flag) => `<span>⚠ ${escapeHtml(flag)}</span>`).join("")}</div>` : '<div class="risk-clear"><span aria-hidden="true">✓</span> No risk rules were triggered.</div>'}
    </div>
    <div class="result-detail" id="${detailId}" hidden>
      <section class="result-block" aria-labelledby="evidence-title-${ticker}"><div class="block-heading"><div><span class="eyebrow">Evidence</span><h3 id="evidence-title-${ticker}">What the researchers found</h3></div><p>Signals summarize the direction of available evidence; open each source below to verify the underlying news.</p></div><div class="grid-3">${evidenceHtml}</div>${skippedResearch.length ? `<p class="hint">${escapeHtml(profile.label)} depth: ${skippedResearch.map((key) => EVIDENCE_META[key].title).join(" and ")} research skipped for speed.</p>` : ""}</section>
      <section class="result-block" aria-labelledby="debate-title-${ticker}"><div class="block-heading"><div><span class="eyebrow">Debate</span><h3 id="debate-title-${ticker}">Strongest cases on both sides</h3></div><p>Strength scores describe each argument, not expected return.</p></div><div class="debate"><article class="debate-side bull-side"><div class="mc-title">▲ Bull case</div><div class="mc-score">${Math.round((analysis.bull?.confidence ?? 0) * 100)}% argument strength</div><p class="mc-sum">${escapeHtml(analysis.bull?.summary || analysis.bull_case || "No bull case was returned.")}</p></article><article class="debate-side bear-side"><div class="mc-title">▼ Bear case</div><div class="mc-score">${Math.round((analysis.bear?.confidence ?? 0) * 100)}% risk strength</div><p class="mc-sum">${escapeHtml(analysis.bear?.summary || analysis.bear_case || "No bear case was returned.")}</p></article></div></section>
      <section class="result-block sources-block" aria-labelledby="sources-title-${ticker}"><div class="block-heading"><div><span class="eyebrow">Sources</span><h3 id="sources-title-${ticker}">Check the evidence yourself</h3></div><p>${escapeHtml(providerText(analysis.providers))}</p></div>${renderSources(analysis.source_references)}</section>
      <div class="result-actions">${canAdd ? `<div class="add-row"><label for="qty-${ticker}">Shares</label><input id="qty-${ticker}" type="number" min="1" step="1" value="${defaultQty}"><button class="add-btn" id="add-${ticker}" type="button">Add to Demo Portfolio</button><span class="muted">Prefilled from the suggested size (${fmtUsd(positionSize)})</span><span class="added-note hidden" id="added-${ticker}" role="status"></span></div>` : '<span class="muted">Demo portfolio additions are offered for BUY recommendations.</span>'}<button class="secondary-btn retry-btn" type="button">Retry ${escapeHtml(ticker)}</button></div>
    </div>`;

  const existing = $(`result-${ticker}`);
  if (existing) existing.replaceWith(card); else $("results-list").appendChild(card);
  card.querySelector(".result-summary").addEventListener("click", () => toggleResult(card));
  card.querySelector(".retry-btn").addEventListener("click", () => retryTicker(ticker));
  if (canAdd) $(`add-${ticker}`).addEventListener("click", () => addToPortfolio(ticker, Number(analysis.price)));
  const entry = state.tickers.get(ticker);
  if (entry) entry.analysis = analysis;
}

function toggleResult(card, forceOpen = null) {
  const summary = card.querySelector(".result-summary");
  const detail = card.querySelector(".result-detail");
  const open = forceOpen == null ? summary.getAttribute("aria-expanded") !== "true" : forceOpen;
  summary.setAttribute("aria-expanded", String(open));
  detail.hidden = !open;
  card.classList.toggle("open", open);
}

function openResult(ticker) {
  const card = $(`result-${ticker}`);
  if (!card) return;
  toggleResult(card, true);
  card.querySelector(".result-summary").focus();
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ---------- portfolio ---------- */

async function addToPortfolio(ticker, price) {
  const button = $(`add-${ticker}`);
  const qtyInput = $(`qty-${ticker}`);
  const note = $(`added-${ticker}`);
  const quantity = Number(qtyInput.value);
  if (!quantity || quantity <= 0) { showToast("Enter a share quantity first", true); qtyInput.focus(); return; }
  button.disabled = true;
  try {
    const response = await fetch("/api/portfolio/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticker, quantity, entry_price: price }) });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `failed (${response.status})`);
    }
    note.textContent = `✓ ${quantity} ${ticker} @ $${price.toFixed(2)} added`;
    note.classList.remove("hidden");
    showToast(`${ticker} added to demo portfolio`);
  } catch (error) {
    showToast(`Could not add position: ${error.message}`, true);
    button.disabled = false;
  }
}

/* ---------- utilities ---------- */

function fmtUsd(value) { return `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`; }

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" }).format(date);
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function safeUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_) { return ""; }
}

function getClientId() {
  try {
    const existing = localStorage.getItem(CLIENT_ID_KEY);
    if (existing) return existing;
    const created = globalThis.crypto?.randomUUID
      ? globalThis.crypto.randomUUID()
      : `device_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(CLIENT_ID_KEY, created);
    return created;
  } catch (_) {
    return `device_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  }
}

function showError(message) { $("error-msg").textContent = message; $("error-msg").classList.remove("hidden"); }
function hideError() { $("error-msg").classList.add("hidden"); }

let toastTimer = null;
function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  toast.setAttribute("role", isError ? "alert" : "status");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 4200);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function escapeAttr(text) { return escapeHtml(text).replaceAll('"', "&quot;").replaceAll("'", "&#39;"); }
