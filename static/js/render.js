/* Rendering: live progress cards, the summary table, and result cards
   with their trust metadata (P0.1), evidence, sources, and chat wiring. */

import {
  EVIDENCE_META, ICONS, OUTLOOK_LABELS, RESEARCH_KEYS,
} from "./constants.js";
import { activeAgents, depthProfile } from "./options.js";
import { state } from "./state.js";
import { addToPortfolio } from "./portfolio-actions.js";
import { retryTicker } from "./tickers.js";
import { sendChatMessage, setChatOpen, toggleChat } from "./chat.js";
import {
  $, escapeAttr, escapeHtml, fmtUsd, formatDate, formatDateTime, safeUrl,
} from "./util.js";

export function labelFor(agent, signal, confidence) {
  if (agent === "bull" || agent === "bear" || agent.endsWith("_rebuttal")) return `${Math.round((confidence ?? 0) * 100)}%`;
  const map = { bullish: "Bullish", bearish: "Bearish", neutral: "Neutral", positive: "Positive", negative: "Negative", unknown: "n/a" };
  return map[signal] || signal || "n/a";
}

export function convictionLabel(confidence) {
  const pct = Math.round((confidence || 0) * 100);
  if (pct >= 70) return "Strong evidence";
  if (pct >= 50) return "Moderate evidence";
  return "Low evidence";
}

// Historical outcome rates for this decision + evidence bucket, or an honest
// "unavailable" while the mature sample is too small (P0.1 -> P1.1). Fetched
// after the card renders so grading data never delays the decision brief.
function fetchTrackRecord(analysis) {
  if (analysis.error) return;
  const params = new URLSearchParams({
    decision: analysis.decision || "HOLD",
    confidence: String(analysis.confidence ?? 0),
    outlook: state.outlook,
    depth: state.depth,
  });
  const pct = (value) => `${Math.round(Number(value) * 100)}%`;
  const signedPct = (value) => `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(1)}%`;
  fetch(`/api/calibration?${params}`)
    .then((response) => (response.ok ? response.json() : null))
    .then((record) => {
      const cell = $(`track-record-${analysis.ticker}`);
      if (!cell || !record) return;
      if (!record.available) {
        cell.textContent = `Track record unavailable · ${record.n_mature} mature call${record.n_mature === 1 ? "" : "s"} (${record.min_observations} needed)`;
        return;
      }
      cell.textContent = record.decision === "HOLD"
        ? `Track record: missed upside ${pct(record.missed_upside_rate)} · avoided downside ${pct(record.avoided_downside_rate)} · n=${record.n_mature}`
        : `Track record: ${pct(record.directional_hit_rate)} directional hit · mean alpha ${signedPct(record.mean_alpha_pct)} · n=${record.n_mature}`;
    })
    .catch(() => {});
}

// Compact signal split from the analyst results themselves - no extra LLM call.
function signalSplit(analysis) {
  const counts = { bullish: 0, neutral: 0, bearish: 0 };
  let available = 0;
  for (const key of RESEARCH_KEYS) {
    const signal = analysis?.[key]?.signal;
    if (!signal) continue;
    available += 1;
    if (signal === "bullish" || signal === "positive") counts.bullish += 1;
    else if (signal === "bearish" || signal === "negative") counts.bearish += 1;
    else counts.neutral += 1;
  }
  return { counts, available };
}

function splitLabel(split) {
  return `${split.counts.bullish} bullish / ${split.counts.neutral} neutral / ${split.counts.bearish} bearish`;
}

// Data age is recomputed at view time from as_of, so a restored or cached run
// never looks fresher than it is. Staleness reuses the outlook-specific
// threshold the server recorded in data_quality.
function dataAgeHours(analysis) {
  if (!analysis.as_of) return null;
  const stamp = new Date(analysis.as_of);
  if (Number.isNaN(stamp.getTime())) return null;
  return Math.max(0, (Date.now() - stamp.getTime()) / 3600000);
}

function ageLabel(hours) {
  if (hours == null) return "Unknown";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m old`;
  if (hours < 48) return `${hours.toFixed(1)}h old`;
  return `${Math.round(hours / 24)}d old`;
}

function staleInfo(analysis) {
  const threshold = analysis.data_quality?.stale_after_hours;
  const hours = dataAgeHours(analysis);
  return { hours, stale: threshold != null && hours != null && hours > threshold };
}

// Falls back to the live depth profile for runs persisted before data_quality.
function coverageInfo(analysis) {
  const dq = analysis.data_quality;
  const split = signalSplit(analysis);
  const serverRecorded = Array.isArray(dq?.expected_analysts) && dq.expected_analysts.length > 0;
  return {
    expected: serverRecorded ? dq.expected_analysts.length : depthProfile().research.length,
    available: serverRecorded ? (dq.available_analysts || []).length : split.available,
    split,
  };
}

// The deterministic risk gate can override the manager; show both calls.
function gateChanged(analysis) {
  return Boolean(analysis.manager_decision && analysis.manager_decision !== analysis.decision);
}

function gateLine(analysis) {
  if (!gateChanged(analysis)) return "";
  return `Manager: ${analysis.manager_decision} -> Final: ${analysis.decision}`;
}

function downgradeFlag(analysis) {
  return (analysis.risk_flags || []).find((flag) => String(flag).startsWith("downgraded")) || "";
}

function forecastBandNote(analysis) {
  // Noise-band context so a red -2% forecast next to a BUY stops looking
  // contradictory: inside +/-1 sigma it is statistical noise, not a signal.
  if (analysis.forecast_band_pct == null) return "";
  const z = analysis.forecast_z;
  const beyond = z != null && Math.abs(z) >= 1;
  return ` · noise ±${Number(analysis.forecast_band_pct).toFixed(1)}%${z == null ? "" : ` (z ${z >= 0 ? "+" : ""}${Number(z).toFixed(2)})`}${beyond ? " · beyond band" : ""}`;
}

function decisionMeta(decision) {
  return { BUY: { cls: "buy", icon: "▲" }, HOLD: { cls: "hold", icon: "■" }, SELL: { cls: "sell", icon: "▼" } }[decision] || { cls: "hold", icon: "■" };
}

export function decisionBadge(analysis) {
  const meta = decisionMeta(analysis.decision);
  return `<span class="decision ${meta.cls}"><span class="d-icon" aria-hidden="true">${meta.icon}</span>${escapeHtml(analysis.decision || "HOLD")}</span>`;
}

export function renderSummaryTable() {
  const analyses = [...state.tickers.values()].map((entry) => entry.analysis).filter(Boolean);
  const panel = $("summary-panel");
  panel.innerHTML = "";
  if (!analyses.length) return;
  const table = document.createElement("table");
  table.id = "summary-table";
  table.innerHTML = `
    <caption class="sr-only">Decision summary for analyzed tickers</caption>
    <thead><tr><th scope="col">Ticker</th><th scope="col">Call</th><th scope="col" class="num">Price</th><th scope="col">Horizon</th><th scope="col">Data age</th><th scope="col">Coverage</th><th scope="col">Risk</th><th scope="col"><span class="sr-only">Action</span></th></tr></thead>
    <tbody>${analyses.map((analysis) => {
      const flags = analysis.risk_flags || [];
      const entry = state.tickers.get(analysis.ticker);
      const age = staleInfo(analysis);
      const cov = coverageInfo(analysis);
      const gated = gateChanged(analysis);
      const gateFlag = downgradeFlag(analysis);
      const riskCell = analysis.error
        ? '<span class="flag-warn">⚠ Unavailable</span>'
        : gated
          ? `<span class="flag-warn" title="${escapeAttr(gateFlag)}">⚠ ${escapeHtml(gateFlag)}</span>`
          : flags.length
            ? `<span class="flag-warn">⚠ ${flags.length} flag${flags.length > 1 ? "s" : ""}</span>`
            : '<span class="flag-ok">✓ Clear</span>';
      return `<tr>
        <td data-label="Ticker"><strong>${escapeHtml(analysis.ticker)}</strong></td>
        <td data-label="Call">${decisionBadge(analysis)}${gated ? `<span class="gate-chip">risk-adjusted</span>` : ""}<small>${convictionLabel(analysis.confidence)} · ${Math.round((analysis.confidence || 0) * 100)}%</small></td>
        <td data-label="Price" class="num">${analysis.price != null ? `$${Number(analysis.price).toFixed(2)}` : "Not available"}</td>
        <td data-label="Horizon">${OUTLOOK_LABELS[state.outlook] || state.outlook}<small>${depthProfile().label} depth</small></td>
        <td data-label="Data age">${age.stale ? '<span class="flag-warn">' : ""}${ageLabel(age.hours)}${age.stale ? " · stale</span>" : ""}<small>${analysis.as_of ? escapeHtml(formatDateTime(analysis.as_of)) : "no timestamp"}${entry?.cached ? " · cached" : ""}</small></td>
        <td data-label="Coverage" class="cov">${analysis.error ? "n/a" : `${cov.available}/${cov.expected} analysts`}${!analysis.error && cov.split.available ? `<small>${splitLabel(cov.split)}</small>` : ""}</td>
        <td data-label="Risk">${riskCell}</td>
        <td data-label="Action"><button class="table-open-btn" type="button" data-open-ticker="${escapeAttr(analysis.ticker)}">View evidence</button></td>
      </tr>`;
    }).join("")}</tbody>`;
  $("summary-panel").appendChild(table);
  $("summary-panel").insertAdjacentHTML("beforeend", '<p class="results-help"><strong>Confidence = evidence strength, not profit odds.</strong> Educational simulation, not advice.</p>');
  $("summary-panel").querySelectorAll("[data-open-ticker]").forEach((button) => button.addEventListener("click", () => openResult(button.dataset.openTicker)));
}

export function renderProgressCard(ticker) {
  const card = document.createElement("article");
  card.className = "ticker-card";
  card.id = `live-${ticker}`;
  card.setAttribute("aria-labelledby", `live-title-${ticker}`);
  const agents = activeAgents();
  card.innerHTML = `
    <div class="ticker-head"><span class="tk" id="live-title-${ticker}">${escapeHtml(ticker)} <span class="px" id="px-${ticker}">fetching data…</span></span><span class="src" id="src-${ticker}"></span><span class="muted prog-count" id="progc-${ticker}">0/${agents.length}</span></div>
    <div class="run-progress" id="progress-${ticker}" role="progressbar" aria-label="${escapeAttr(ticker)} analysis progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="run-progress-fill" id="prog-${ticker}"></div></div>
    ${agents.map((agent) => `
      <div class="agent-cell" id="cell-${ticker}-${agent.key}">
        <div class="agent-row ${agent.stage !== "Research" ? "stage2" : ""}" data-agent="${agent.key}">
          <span class="agent-left"><span class="agent-icon">${ICONS[agent.key]}</span><span>${agent.label}<small>${agent.stage}</small></span></span>
          <span class="stream-hint" hidden>reasoning ▾</span>
          <span class="status" id="status-${ticker}-${agent.key}"><span class="icon" aria-hidden="true"></span>Waiting</span>
        </div>
        <div class="stream-pane" id="stream-${ticker}-${agent.key}" hidden><pre id="stream-pre-${ticker}-${agent.key}"></pre></div>
      </div>`).join("")}`;
  $("live-grid").appendChild(card);
  // Once an agent streams tokens, its row becomes a toggle for the pane.
  card.querySelectorAll(".agent-row[data-agent]").forEach((row) => {
    const toggle = () => toggleStreamPane(ticker, row.dataset.agent);
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });
  });
}

export function toggleStreamPane(ticker, agentKey) {
  const cell = $(`cell-${ticker}-${agentKey}`);
  const pane = $(`stream-${ticker}-${agentKey}`);
  if (!cell || !pane || !cell.classList.contains("has-stream")) return;
  const open = pane.hidden;
  pane.hidden = !open;
  cell.classList.toggle("open", open);
  const row = cell.querySelector(".agent-row");
  row.setAttribute("aria-expanded", String(open));
  const hint = row.querySelector(".stream-hint");
  if (hint) hint.textContent = open ? "reasoning ▴" : "reasoning ▾";
  if (open) pane.scrollTop = pane.scrollHeight;
}

export function setHeader(ticker, price, name, sources) {
  const priceElement = $(`px-${ticker}`);
  const sourceElement = $(`src-${ticker}`);
  if (priceElement && price != null) priceElement.textContent = `$${Number(price).toFixed(2)}`;
  else if (priceElement && name) priceElement.textContent = name;
  if (sourceElement && sources) {
    const forecastSource = sources.forecast === "timegpt" ? "Nixtla TimeGPT" : sources.forecast === "local" ? "local forecast" : null;
    const socialSource = sources.social === "olostep" ? "reddit/stocktwits" : null;
    const unique = [...new Set([sources.prices, sources.fundamentals, sources.news, socialSource, forecastSource].filter((source) => source && source !== "none"))];
    sourceElement.textContent = unique.length ? `via ${unique.join(" + ")}` : "";
  }
}

export function setAgentStatus(ticker, agent, statusClass, text, duration) {
  const element = $(`status-${ticker}-${agent}`);
  if (!element) return;
  element.className = `status ${statusClass}`;
  const durationText = duration ? `${Number(duration).toFixed(1)}s` : "";
  // Visual progress only: the overall status live region announces ticker
  // results, so per-agent starts/completions are not spoken (P0.3).
  element.innerHTML = `<span class="icon" aria-hidden="true"></span><span class="status-label">${escapeHtml(text)}</span>${durationText ? `<span class="status-duration">${durationText}</span>` : ""}`;
}

function signalClass(signal) { return `sig-${String(signal || "unknown").toLowerCase()}`; }

function evidenceCard(title, result, agent) {
  const signal = result?.signal || "unknown";
  return `<article class="evidence-card"><div class="mc-title">${escapeHtml(title)}</div><div class="mc-sig ${signalClass(signal)}">${escapeHtml(labelFor(agent, signal, result?.confidence))}</div><p class="mc-sum">${escapeHtml(result?.summary || "No evidence returned.")}</p></article>`;
}

function providerText(providers) {
  const values = Object.entries(providers || {}).filter(([, value]) => value && value !== "none");
  return values.length ? values.map(([kind, provider]) => `${kind}: ${provider}`).join(" · ") : "Provider metadata unavailable";
}

function renderSources(references) {
  const sources = (references || []).filter((source) => source && source.title);
  if (!sources.length) return '<p class="empty-sources">No linked sources for this run.</p>';
  return `<ul class="source-list">${sources.map((source) => {
    const url = safeUrl(source.url);
    const title = escapeHtml(source.title);
    const titleMarkup = url ? `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${title}<span aria-hidden="true"> ↗</span><span class="sr-only"> (opens in a new tab)</span></a>` : `<span>${title}</span>`;
    return `<li>${titleMarkup}<div>${escapeHtml(source.provider || "Unknown publisher")}${source.published_at ? ` · ${escapeHtml(formatDate(source.published_at))}` : ""}</div></li>`;
  }).join("")}</ul>`;
}

function renderTrackRecord(analysis, ticker) {
  const decisions = analysis.past_decisions || [];
  if (!decisions.length) return "";
  const pct = (value) => `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(1)}%`;
  const rows = decisions.map((d) => {
    const graded = d.realized_return_pct != null;
    const outcome = graded
      ? `<strong>${pct(d.realized_return_pct)}</strong> over ${Number(d.window_days)}d${d.alpha_vs_spy_pct != null ? ` · SPY ${pct(d.spy_return_pct)} · alpha <strong>${pct(d.alpha_vs_spy_pct)}</strong>` : ""}${d.mature ? "" : " · partial window"}`
      : "outcome pending";
    return `<li><div class="memory-line"><span>${escapeHtml(d.date)} ${decisionBadge(d)} at $${Number(d.price_at_decision).toFixed(2)}</span><span class="memory-outcome">${outcome}</span></div><small>${escapeHtml(d.reflection || "")}</small></li>`;
  }).join("");
  return `<section class="result-block" aria-labelledby="memory-title-${ticker}"><div class="block-heading"><h3 id="memory-title-${ticker}">Past calls</h3></div><ul class="memory-list">${rows}</ul></section>`;
}

export function renderResultCard(analysis) {
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
  // Runs saved before the Sentiment analyst shipped have no "social" provider key.
  const sentimentResult = analysis.sentiment
    || (analysis.providers && !("social" in analysis.providers)
      ? { signal: "unknown", confidence: 0, summary: "This run predates the Sentiment analyst, so no social reading was recorded." }
      : null);
  const canAdd = analysis.decision === "BUY" && analysis.price && !analysis.error;
  const positionSize = analysis.suggested_size_usd || 10000;
  const defaultQty = analysis.price ? Math.max(1, Math.floor(positionSize / analysis.price)) : 0;
  const asOf = analysis.as_of ? formatDateTime(analysis.as_of) : "Timestamp unavailable";
  const age = staleInfo(analysis);
  const cov = coverageInfo(analysis);
  const gated = gateChanged(analysis);
  const cachedRun = Boolean(state.tickers.get(ticker)?.cached);
  const fallbacks = analysis.data_quality?.provider_fallbacks || [];
  const tokens = analysis.token_usage || {};
  const tokenFact = tokens.total_tokens
    ? `<div><span>LLM tokens</span><strong>${Number(tokens.total_tokens).toLocaleString()}</strong><small>${Number(tokens.prompt_tokens || 0).toLocaleString()} prompt + ${Number(tokens.completion_tokens || 0).toLocaleString()} completion${tokens.reasoning_tokens ? ` · ${Number(tokens.reasoning_tokens).toLocaleString()} reasoning` : ""}</small></div>`
    : "";
  const profile = depthProfile();
  const skippedResearch = Object.keys(EVIDENCE_META).filter((key) => !profile.research.includes(key));
  const evidenceHtml = profile.research.map((key) => evidenceCard(
    EVIDENCE_META[key].title,
    key === "forecast" ? forecastResult : analysis[key],
    key,
  )).join("");
  const conditions = analysis.would_upgrade_if || analysis.would_downgrade_if
    ? `<div class="manager-conditions"><span class="eyebrow eyebrow-flat">Conditions for a different call - not alerts or price targets</span>${analysis.would_upgrade_if ? `<p><strong>Stronger call if:</strong> ${escapeHtml(analysis.would_upgrade_if)}</p>` : ""}${analysis.would_downgrade_if ? `<p><strong>Weaker call if:</strong> ${escapeHtml(analysis.would_downgrade_if)}</p>` : ""}</div>`
    : "";

  card.innerHTML = `
    <button class="result-summary" type="button" aria-expanded="false" aria-controls="${detailId}"><span class="result-identity"><span class="tk">${escapeHtml(ticker)}</span><span class="company">${escapeHtml(analysis.company_name || "Company name unavailable")}</span></span>${decisionBadge(analysis)}${gated ? '<span class="gate-chip">risk-adjusted</span>' : ""}<span class="summary-action">Evidence &amp; sources <span class="caret" aria-hidden="true">▶</span></span></button>
    <div class="decision-brief">
      ${gated ? `<div class="gate-banner"><span class="gate-chip">risk-adjusted</span><span class="gate-line">${escapeHtml(gateLine(analysis))}</span>${downgradeFlag(analysis) ? `<small>⚠ ${escapeHtml(downgradeFlag(analysis))}</small>` : ""}</div>` : ""}
      <div class="decision-facts"><div><span>Current price</span><strong>${analysis.price != null ? `$${Number(analysis.price).toFixed(2)}` : "Unavailable"}</strong></div><div><span>5-day forecast</span><strong>${analysis.forecast_price_5d != null ? `$${Number(analysis.forecast_price_5d).toFixed(2)} (${Number(analysis.forecast_change_5d_pct) >= 0 ? "+" : ""}${Number(analysis.forecast_change_5d_pct).toFixed(2)}%)` : "Unavailable"}</strong><small>${analysis.forecast_method === "timegpt-1" ? "TimeGPT" : analysis.forecast_trend_r2 != null ? `Local fit R² ${Number(analysis.forecast_trend_r2).toFixed(2)}` : "Local"}${forecastBandNote(analysis)}</small></div><div><span>Data age</span><strong>${age.stale ? '<span class="flag-warn">' : ""}${ageLabel(age.hours)}${cachedRun ? " · cached" : ""}${age.stale ? " · stale</span>" : ""}</strong><small>${escapeHtml(asOf)}</small></div><div><span>Evidence strength</span><strong>${convictionLabel(analysis.confidence)} · ${confidencePct}%</strong><small id="track-record-${ticker}" class="track-record"></small></div><div><span>Horizon</span><strong>${OUTLOOK_LABELS[state.outlook] || state.outlook}</strong><small>${profile.label} depth</small></div><div><span>Analyst coverage</span><strong>${analysis.error ? "n/a" : `${cov.available}/${cov.expected} analysts`}</strong>${!analysis.error && cov.split.available ? `<small>${splitLabel(cov.split)}</small>` : ""}${fallbacks.length ? `<small class="flag-warn">${fallbacks.map(escapeHtml).join(" · ")}</small>` : ""}</div><div><span>Suggested size</span><strong>${analysis.suggested_size_usd ? fmtUsd(analysis.suggested_size_usd) : "No position"}</strong></div>${tokenFact}</div>
      <div class="manager-conclusion"><span class="eyebrow">Manager conclusion</span><p class="thesis">${escapeHtml(analysis.summary || analysis.error || "No manager summary was returned.")}</p></div>
      ${conditions}
      ${analysis.error ? `<div class="risk-flags"><strong>Analysis unavailable</strong><span>⚠ ${escapeHtml(analysis.error)}</span></div>` : flags.length ? `<div class="risk-flags"><strong>Risk flags</strong>${flags.map((flag) => `<span>⚠ ${escapeHtml(flag)}</span>`).join("")}</div>` : '<div class="risk-clear"><span aria-hidden="true">✓</span> No risk rules were triggered.</div>'}
    </div>
    ${analysis.error ? "" : `
    <div class="chat-block">
      <button class="chat-toggle" id="chat-toggle-${ticker}" type="button" aria-expanded="false" aria-controls="chat-panel-${ticker}">
        <span class="chat-toggle-icon" aria-hidden="true">${ICONS.manager}</span>Chat with Portfolio Manager
      </button>
      <div class="chat-panel" id="chat-panel-${ticker}" hidden>
        <div class="chat-messages" id="chat-msgs-${ticker}" role="log" aria-label="Conversation with the portfolio manager about ${escapeAttr(ticker)}"></div>
        <form class="chat-form" id="chat-form-${ticker}">
          <input id="chat-input-${ticker}" type="text" maxlength="2000" autocomplete="off"
                 placeholder="Ask anything, e.g. should I invest in this outside my portfolio?"
                 aria-label="Question for the portfolio manager about ${escapeAttr(ticker)}">
          <button class="chat-send" id="chat-send-${ticker}" type="submit">Send</button>
        </form>
        <p class="chat-hint">Grounded in this run's research · educational simulation, not advice</p>
      </div>
    </div>`}
    <div class="result-detail" id="${detailId}" hidden>
      <section class="result-block" aria-labelledby="debate-title-${ticker}"><div class="block-heading"><h3 id="debate-title-${ticker}">Bull vs bear</h3></div><div class="debate"><article class="debate-side bull-side"><div class="mc-title">▲ Bull case</div><div class="mc-score">${Math.round((analysis.bull?.confidence ?? 0) * 100)}% argument strength</div><p class="mc-sum">${escapeHtml(analysis.bull?.summary || analysis.bull_case || "No bull case was returned.")}</p></article><article class="debate-side bear-side"><div class="mc-title">▼ Bear case</div><div class="mc-score">${Math.round((analysis.bear?.confidence ?? 0) * 100)}% risk strength</div><p class="mc-sum">${escapeHtml(analysis.bear?.summary || analysis.bear_case || "No bear case was returned.")}</p></article></div></section>
      <section class="result-block" aria-labelledby="evidence-title-${ticker}"><div class="block-heading"><h3 id="evidence-title-${ticker}">Analyst evidence</h3></div><div class="grid-3">${evidenceHtml}</div>${skippedResearch.length ? `<p class="hint">Skipped for speed: ${skippedResearch.map((key) => EVIDENCE_META[key].title).join(" · ")}</p>` : ""}</section>
      <section class="result-block sources-block" aria-labelledby="sources-title-${ticker}"><div class="block-heading"><h3 id="sources-title-${ticker}">Sources</h3><p>${escapeHtml(providerText(analysis.providers))}</p></div>${renderSources(analysis.source_references)}</section>
      ${renderTrackRecord(analysis, ticker)}
      <div class="result-actions">${canAdd ? `<div class="add-row"><label for="qty-${ticker}">Shares</label><input id="qty-${ticker}" type="number" min="1" step="1" value="${defaultQty}"><button class="add-btn" id="add-${ticker}" type="button">Add to Demo Portfolio</button><span class="muted">suggests ${fmtUsd(positionSize)}</span><span class="added-note hidden" id="added-${ticker}" role="status"></span></div>` : '<span class="muted">Portfolio adds are offered on BUY calls.</span>'}<button class="secondary-btn retry-btn" type="button">Retry ${escapeHtml(ticker)}</button></div>
    </div>`;

  const existing = $(`result-${ticker}`);
  if (existing) existing.replaceWith(card); else $("results-list").appendChild(card);
  card.querySelector(".result-summary").addEventListener("click", () => toggleResult(card));
  // Escape closes the innermost open panel and returns focus to the control
  // that opened it (P0.3).
  card.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const chatPanel = $(`chat-panel-${ticker}`);
    if (chatPanel && !chatPanel.hidden) {
      setChatOpen(ticker, false);
      return;
    }
    const summary = card.querySelector(".result-summary");
    if (summary.getAttribute("aria-expanded") === "true") {
      toggleResult(card, false);
      summary.focus();
    }
  });
  card.querySelector(".retry-btn").addEventListener("click", () => retryTicker(ticker));
  if (canAdd) $(`add-${ticker}`).addEventListener("click", () => addToPortfolio(ticker, Number(analysis.price)));
  if (!analysis.error) {
    $(`chat-toggle-${ticker}`).addEventListener("click", () => toggleChat(ticker));
    $(`chat-form-${ticker}`).addEventListener("submit", (event) => {
      event.preventDefault();
      sendChatMessage(ticker);
    });
    // A re-render (hydration, ticker_completed replay) must not lose the chat.
    if (state.chats.get(ticker)?.open) setChatOpen(ticker, true);
  }
  const entry = state.tickers.get(ticker);
  if (entry) entry.analysis = analysis;
  fetchTrackRecord(analysis);
}

export function toggleResult(card, forceOpen = null) {
  const summary = card.querySelector(".result-summary");
  const detail = card.querySelector(".result-detail");
  const open = forceOpen == null ? summary.getAttribute("aria-expanded") !== "true" : forceOpen;
  summary.setAttribute("aria-expanded", String(open));
  detail.hidden = !open;
  card.classList.toggle("open", open);
}

export function openResult(ticker) {
  const card = $(`result-${ticker}`);
  if (!card) return;
  toggleResult(card, true);
  card.querySelector(".result-summary").focus();
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}
