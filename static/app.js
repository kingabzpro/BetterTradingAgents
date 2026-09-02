/* BetterTradingAgents - main page logic (vanilla JS, no dependencies). */

const ICONS = {
  technical: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12.5 6 8l2.5 2.5L13.5 4"/></svg>',
  fundamental: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M3 3.5h10M3 7h10M3 10.5h6"/><circle cx="12.4" cy="10.7" r="1.6"/></svg>',
  news: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="5.7"/><path d="M2.3 8h11.4M8 2.3c-1.8 1.6-2.7 3.5-2.7 5.7s.9 4.1 2.7 5.7c1.8-1.6 2.7-3.5 2.7-5.7S9.8 3.9 8 2.3z"/></svg>',
  bull: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 12.5 12.5 3.5M6.5 3.5h6v6"/></svg>',
  bear: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3.5l9 9M12.5 6.5v6h-6"/></svg>',
  bull_rebuttal: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 12.5 12.5 3.5M6.5 3.5h6v6"/><path d="M2.5 5.5h3M2.5 8h2"/></svg>',
  bear_rebuttal: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3.5l9 9M12.5 6.5v6h-6"/><path d="M2.5 5.5h3M2.5 8h2"/></svg>',
  manager: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5" width="11" height="8" rx="2"/><path d="M6 5V3.6A1.6 1.6 0 0 1 7.6 2h.8A1.6 1.6 0 0 1 10 3.6V5M2.5 8.5h11"/></svg>',
};

const AGENTS = [
  { key: "technical", label: "Technical" },
  { key: "fundamental", label: "Fundamentals" },
  { key: "news", label: "News" },
  { key: "bull", label: "Bull", stage2: true },
  { key: "bear", label: "Bear", stage2: true },
  { key: "bull_rebuttal", label: "Bull Rebuttal", stage2: true, rebuttal: true },
  { key: "bear_rebuttal", label: "Bear Rebuttal", stage2: true, rebuttal: true },
  { key: "manager", label: "Portfolio Manager", stage2: true },
];

const $ = (id) => document.getElementById(id);
const state = { running: false, es: null, tickers: new Map(), runStartedAt: null, timer: null, debateRounds: 1 };

/* ---------- boot ---------- */

document.addEventListener("DOMContentLoaded", () => {
  $("analyze-btn").addEventListener("click", startAnalysis);
  $("ticker-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") startAnalysis();
  });
  document.querySelectorAll(".chip-btn").forEach((chip) => {
    chip.addEventListener("click", () => {
      $("ticker-input").value = chip.dataset.tickers;
      $("ticker-input").focus();
    });
  });
  fetch("/api/health")
    .then((response) => response.json())
    .then((health) => {
      if (health.mock_mode) $("mode-chip").classList.remove("hidden");
      state.debateRounds = health.debate_rounds || 1;
      const providers = health.providers || {};
      $("provider-line").textContent =
        `data: ${providers.prices || "?"} prices, ${providers.fundamentals || "?"} fundamentals, ` +
        `${providers.news_search || "?"} news search. model: ${health.llm_model || "mock"}`;
    })
    .catch(() => {});
});

/* ---------- analysis ---------- */

async function startAnalysis() {
  if (state.running) return;
  const input = $("ticker-input").value.trim();
  if (!input) return;

  const tickers = [...new Set(input.split(",").map((t) => t.trim().toUpperCase()).filter(Boolean))];
  if (tickers.length > 5) { showError(`Max 5 tickers at once (you entered ${tickers.length}).`); return; }

  hideError();
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tickers }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || `Analysis failed (${response.status})`);
      return;
    }
    const { run_id: runId } = await response.json();
    beginRun(tickers);
    openStream(runId);
  } catch (error) {
    showError(`Could not reach the server: ${error.message}`);
  }
}

function beginRun(tickers) {
  state.running = true;
  state.tickers = new Map(tickers.map((ticker) => [ticker, { agents: {} }]));
  state.runStartedAt = performance.now();
  $("analyze-btn").disabled = true;
  $("how-section").classList.add("hidden");
  $("results-section").classList.add("hidden");
  $("results-list").innerHTML = "";
  $("live-section").classList.remove("hidden");
  $("live-grid").innerHTML = "";
  for (const ticker of tickers) renderProgressCard(ticker);
  state.timer = window.setInterval(() => {
    const seconds = ((performance.now() - state.runStartedAt) / 1000).toFixed(1);
    $("run-timer").textContent = `· ${seconds}s`;
  }, 100);
}

function openStream(runId) {
  if (state.es) state.es.close();
  const source = new EventSource(`/api/runs/${runId}/events`);
  state.es = source;
  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    handleEvent(data);
    if (data.type === "analysis_completed") {
      source.close();
      state.es = null;
      finishRun();
    }
  };
  source.onerror = () => {
    source.close();
    if (state.running) {
      state.es = null;
      finishRun();
      showToast("Stream interrupted, showing results gathered so far", true);
    }
  };
}

function finishRun() {
  state.running = false;
  $("analyze-btn").disabled = false;
  window.clearInterval(state.timer);
  const seconds = ((performance.now() - state.runStartedAt) / 1000).toFixed(1);
  $("run-timer").textContent = `· done in ${seconds}s`;
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
      const detail = event.signal ? ` · ${labelFor(event.agent, event.signal, event.confidence)}` : "";
      setAgentStatus(ticker, event.agent, "done", `Complete ✓${detail}`, event.duration_s);
      if (event.summary) entry.agents[event.agent] = { ...entry.agents[event.agent], signal: event.signal, confidence: event.confidence, summary: event.summary };
      break;
    }
    case "agent_failed":
      setAgentStatus(ticker, event.agent, "failed", "Failed ⚠");
      break;
    case "ticker_failed":
      setHeader(ticker, null, `failed: ${event.error || "market data unavailable"}`, null);
      break;
    case "ticker_completed":
      entry.analysis = event.analysis;
      entry.agents = {
        technical: event.analysis.technical, fundamental: event.analysis.fundamental,
        news: event.analysis.news, bull: event.analysis.bull, bear: event.analysis.bear,
        manager: { signal: event.decision, confidence: event.confidence, summary: event.analysis.summary },
      };
      renderResultCard(event.analysis);
      break;
  }
}

function labelFor(agent, signal, confidence) {
  if (agent === "bull" || agent === "bear" || agent.endsWith("_rebuttal")) {
    return `${Math.round((confidence ?? 0) * 100)}%`;
  }
  const map = { bullish: "Bullish", bearish: "Bearish", neutral: "Neutral", positive: "Positive", negative: "Negative", unknown: "n/a" };
  return map[signal] || signal;
}

/* ---------- rendering ---------- */

function renderProgressCard(ticker) {
  const card = document.createElement("div");
  card.className = "ticker-card";
  card.id = `live-${ticker}`;
  const agents = state.debateRounds >= 2 ? AGENTS : AGENTS.filter((agent) => !agent.rebuttal);
  card.innerHTML = `
    <div class="ticker-head">
      <span class="tk">${ticker} <span class="px" id="px-${ticker}">fetching data…</span></span>
      <span class="src" id="src-${ticker}"></span>
    </div>
    ${agents.map(
      (agent) => `
      <div class="agent-row ${agent.stage2 ? "stage2" : ""}">
        <span class="agent-left"><span class="agent-icon">${ICONS[agent.key]}</span>${agent.label}</span>
        <span class="status" id="status-${ticker}-${agent.key}"><span class="icon"></span>Waiting</span>
      </div>`
    ).join("")}
  `;
  $("live-grid").appendChild(card);
}

function setHeader(ticker, price, name, sources) {
  const priceElement = $(`px-${ticker}`);
  const sourceElement = $(`src-${ticker}`);
  if (priceElement && price != null) {
    priceElement.textContent = `$${Number(price).toFixed(2)}`;
  } else if (priceElement && name && name.startsWith("failed:")) {
    priceElement.textContent = name;
  } else if (priceElement && name) {
    priceElement.textContent = name;
  }
  if (sourceElement && sources) {
    const unique = [...new Set([sources.prices, sources.fundamentals, sources.news].filter(Boolean))];
    sourceElement.textContent = unique.length ? `via ${unique.join(" + ")}` : "";
  }
}

function setAgentStatus(ticker, agent, statusClass, text, duration) {
  const element = $(`status-${ticker}-${agent}`);
  if (!element) return;
  element.className = `status ${statusClass}`;
  element.innerHTML = `<span class="icon"></span>${text}${duration ? ` <span class="muted">${duration}s</span>` : ""}`;
}

function signalClass(signal) {
  return `sig-${signal || "unknown"}`;
}

function miniCard(title, signal, scoreText, summary) {
  return `
    <div class="mini-card">
      <div class="mc-title">${title}</div>
      <div class="mc-sig ${signalClass(signal)}">${scoreText}</div>
      <div class="mc-sum">${escapeHtml(summary || "")}</div>
    </div>`;
}

function renderResultCard(analysis) {
  $("results-section").classList.remove("hidden");
  const card = document.createElement("div");
  card.className = "result-card";
  card.id = `result-${analysis.ticker}`;

  const decisionMeta = {
    BUY: { cls: "buy", icon: "▲" },
    HOLD: { cls: "hold", icon: "■" },
    SELL: { cls: "sell", icon: "▼" },
  }[analysis.decision] || { cls: "hold", icon: "■" };
  const confidencePct = Math.round((analysis.confidence || 0) * 100);
  const canAdd = analysis.decision === "BUY" && analysis.price;
  const positionSize = analysis.suggested_size_usd || 10000;
  const defaultQty = analysis.price ? Math.floor(positionSize / analysis.price) : 0;

  card.innerHTML = `
    <div class="result-summary" onclick="this.parentElement.classList.toggle('open')">
      <span class="tk">${analysis.ticker}</span>
      <span class="decision ${decisionMeta.cls}"><span class="d-icon">${decisionMeta.icon}</span>${analysis.decision}</span>
      <div class="confidence">${confidencePct}%<div class="conf-bar"><span style="width:${confidencePct}%"></span></div></div>
      <span class="company">${escapeHtml(analysis.company_name || "")}</span>
      <span class="dur">${analysis.duration_s}s</span>
      <span class="caret">▶</span>
    </div>
    <div class="result-detail">
      <div class="decision-headline">
        <span class="decision ${decisionMeta.cls}"><span class="d-icon">${decisionMeta.icon}</span>${analysis.decision}</span>
        <span class="muted">${confidencePct}% confidence${analysis.price ? ` · $${Number(analysis.price).toFixed(2)}` : ""}</span>
      </div>
      <p class="thesis">${escapeHtml(analysis.summary || "")}</p>
      ${(analysis.risk_flags || []).length ? `<div class="risk-flags">${analysis.risk_flags.map((f) => `⚠ ${escapeHtml(f)}`).join("<br>")}</div>` : ""}
      <div class="grid-3">
        ${miniCard("Technical", analysis.technical?.signal, labelFor("technical", analysis.technical?.signal), analysis.technical?.summary)}
        ${miniCard("Fundamentals", analysis.fundamental?.signal, labelFor("fundamental", analysis.fundamental?.signal), analysis.fundamental?.summary)}
        ${miniCard("News", analysis.news?.signal, labelFor("news", analysis.news?.signal), analysis.news?.summary)}
      </div>
      <div class="debate">
        <div class="mini-card">
          <div class="mc-title">Bull Case</div>
          <div class="mc-score">strength ${Math.round((analysis.bull?.confidence ?? 0) * 100)}%</div>
          <div class="mc-sum">${escapeHtml(analysis.bull?.summary || analysis.bull_case || "")}</div>
        </div>
        <div class="mini-card bear-side">
          <div class="mc-title">Bear Case</div>
          <div class="mc-score">risk ${Math.round((analysis.bear?.confidence ?? 0) * 100)}%</div>
          <div class="mc-sum">${escapeHtml(analysis.bear?.summary || analysis.bear_case || "")}</div>
        </div>
      </div>
      <div class="add-row">
        ${canAdd
          ? `<label for="qty-${analysis.ticker}">Shares</label>
             <input id="qty-${analysis.ticker}" type="number" min="1" step="1" value="${defaultQty}">
             <button class="add-btn" id="add-${analysis.ticker}" onclick="addToPortfolio('${analysis.ticker}', ${analysis.price})">Add to Demo Portfolio</button>
             <span class="muted">risk-sized ${fmtUsd(positionSize)}</span>
             <span class="added-note hidden" id="added-${analysis.ticker}"></span>`
          : `<span class="muted">Demo portfolio additions are offered for BUY recommendations.</span>`}
      </div>
    </div>
  `;
  const list = $("results-list");
  const existing = $(`result-${analysis.ticker}`);
  if (existing) existing.remove();
  list.appendChild(card);
}

/* ---------- portfolio ---------- */

async function addToPortfolio(ticker, price) {
  const button = $(`add-${ticker}`);
  const qtyInput = $(`qty-${ticker}`);
  const note = $(`added-${ticker}`);
  const quantity = Number(qtyInput.value);
  if (!quantity || quantity <= 0) { showToast("Enter a share quantity first", true); return; }
  button.disabled = true;
  try {
    const response = await fetch("/api/portfolio/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, quantity, entry_price: price }),
    });
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

/* ---------- utils ---------- */

function fmtUsd(value) {
  return `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}

function showError(message) { $("error-msg").textContent = message; $("error-msg").classList.remove("hidden"); }
function hideError() { $("error-msg").classList.add("hidden"); }

let toastTimer = null;
function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 3200);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}
