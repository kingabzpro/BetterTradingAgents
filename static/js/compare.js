/* Compare page: up to three (run, ticker) decisions on the same rows (P1.5).
   Selection state lives in the URL so the view refreshes and shares. */

import {
  $, escapeHtml, escapeAttr, fmtUsd, formatDateTime, getClientId, showToast,
} from "./util.js?v=11";
import { CLIENT_ID_KEY, OUTLOOK_LABELS } from "./constants.js?v=11";

const MAX_ITEMS = 3;
const DEPTH_LABELS = { fast: "Fast", medium: "Medium", expert: "Expert" };
const TICKER_PATTERN = /^[A-Z0-9.\-]{1,10}$/;

let runMeta = new Map(); // run_id -> /api/runs row
let items = []; // [{ runId, ticker, analysis | null }]

document.addEventListener("DOMContentLoaded", () => {
  $("clear-compare").addEventListener("click", () => setItems([]));
  $("add-compare").addEventListener("click", addItem);
  $("add-run").addEventListener("change", fillTickerSelect);
  loadRuns();
});

function parseItems() {
  const raw = new URLSearchParams(window.location.search).get("items") || "";
  const parsed = [];
  for (const pair of raw.split(",")) {
    if (!pair.trim()) continue;
    const [runId, ticker] = pair.split(":");
    if (!runId || !TICKER_PATTERN.test(ticker || "")) continue;
    if (!parsed.some((i) => i.runId === runId && i.ticker === ticker)) {
      parsed.push({ runId, ticker, analysis: null });
    }
    if (parsed.length === MAX_ITEMS) break;
  }
  return parsed;
}

function setItems(next) {
  items = next;
  const url = new URL(window.location.href);
  if (items.length) {
    url.searchParams.set("items", items.map((i) => `${i.runId}:${i.ticker}`).join(","));
  } else {
    url.searchParams.delete("items");
  }
  history.replaceState(null, "", url);
  loadSelection();
}

async function loadRuns() {
  try {
    const response = await fetch("/api/runs?limit=50", {
      headers: { "X-Client-ID": getClientId(CLIENT_ID_KEY) },
    });
    if (!response.ok) throw new Error(`failed (${response.status})`);
    runMeta = new Map((await response.json()).map((run) => [run.run_id, run]));
  } catch (_) {
    runMeta = new Map();
  }
  fillRunSelect();
  loadSelection();
}

function fillRunSelect() {
  const select = $("add-run");
  const runs = [...runMeta.values()].filter((run) => run.status === "completed");
  select.innerHTML = runs.length
    ? runs.map((run) => `<option value="${escapeAttr(run.run_id)}">${escapeHtml(
        `${formatDateTime(run.started_at * 1000)} · ${run.tickers.join(" ")}`
      )}</option>`).join("")
    : '<option value="">No saved runs yet</option>';
  fillTickerSelect();
}

function fillTickerSelect() {
  const run = runMeta.get($("add-run").value);
  const tickers = run ? run.tickers : [];
  $("add-ticker").innerHTML = tickers.length
    ? tickers.map((t) => `<option value="${escapeAttr(t)}">${escapeHtml(t)}</option>`).join("")
    : '<option value="">-</option>';
}

async function addItem() {
  const runId = $("add-run").value;
  const ticker = $("add-ticker").value;
  if (!runId || !ticker) {
    showToast("Pick a run and a ticker first", true);
    return;
  }
  if (items.length >= MAX_ITEMS) {
    showToast(`Compare holds up to ${MAX_ITEMS} decisions`, true);
    return;
  }
  if (items.some((i) => i.runId === runId && i.ticker === ticker)) {
    showToast(`${ticker} from that run is already in the comparison`, true);
    return;
  }
  setItems([...items, { runId, ticker, analysis: null }]);
}

async function loadSelection() {
  items = parseItems();
  $("compare-empty").classList.toggle("hidden", items.length > 0);
  $("clear-compare").disabled = items.length === 0;
  $("add-compare").disabled = items.length >= MAX_ITEMS;
  $("add-hint").textContent = items.length >= MAX_ITEMS
    ? `Compare holds up to ${MAX_ITEMS} decisions. Remove one to add another.`
    : `${items.length} of ${MAX_ITEMS} selected.`;
  if (!items.length) {
    $("compare-status").textContent = "No decisions selected";
    $("compare-list").innerHTML = "";
    $("changed-lead").classList.add("hidden");
    return;
  }
  $("compare-status").textContent = "Loading comparison…";
  const runs = new Map();
  await Promise.all([...new Set(items.map((i) => i.runId))].map(async (runId) => {
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
      if (response.ok) runs.set(runId, await response.json());
    } catch (_) { /* missing run stays missing */ }
  }));
  for (const item of items) {
    const status = runs.get(item.runId);
    item.analysis = status?.results?.[item.ticker] ?? null;
    item.run = runMeta.get(item.runId) || status || null;
  }
  render();
  $("compare-status").textContent = items.length === 1
    ? "1 decision selected · add another to compare"
    : `${items.length} decisions compared`;
}

function render() {
  renderChanged();
  $("compare-list").innerHTML = "";
  const grid = document.createElement("div");
  grid.className = "compare-grid";
  grid.style.setProperty("--cols", String(Math.max(items.length, 1)));
  for (let index = 0; index < items.length; index += 1) {
    grid.appendChild(column(items[index], index));
  }
  $("compare-list").appendChild(grid);
}

function renderChanged() {
  const blocks = items
    .filter((i) => i.analysis?.what_changed?.length)
    .map((i) => `
      <div class="changed-block">
        <strong>${escapeHtml(i.ticker)} · ${escapeHtml(i.run ? formatDateTime(i.run.started_at * 1000) : "run not found")}</strong>
        <p class="muted">vs the earlier call${i.analysis.previous ? ` on ${escapeHtml(formatDateTime(i.analysis.previous.analyzed_at * 1000))}` : ""}</p>
        <ul>${i.analysis.what_changed.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>
      </div>`);
  $("changed-lead").classList.toggle("hidden", blocks.length === 0);
  $("changed-blocks").innerHTML = blocks.join("");
}

function column(item, index) {
  const { ticker, analysis: a, run } = item;
  const card = document.createElement("article");
  card.className = "compare-col";
  const runLine = run
    ? `${formatDateTime(run.started_at * 1000)} · ${run.outlook ? (OUTLOOK_LABELS[run.outlook] || run.outlook) : ""} · ${run.depth ? (DEPTH_LABELS[run.depth] || run.depth) : ""}`
    : "Run not found";
  const facts = a ? [
    ["Run", `${escapeHtml(runLine)}<small>run ${escapeHtml(item.runId)}</small>`],
    ["Final call", `${decisionBadge(a.decision)}<small>${escapeHtml(evidenceLabel(a.confidence))} · ${Math.round((a.confidence || 0) * 100)}%</small>`],
    ["Manager call", a.manager_decision
      ? `${decisionBadge(a.manager_decision)}${a.manager_confidence != null ? `<small>${Math.round(a.manager_confidence * 100)}% evidence before the risk gate</small>` : ""}`
      : '<span class="muted">Not recorded</span>'],
    ["Evidence strength", `<strong>${escapeHtml(evidenceLabel(a.confidence))} · ${Math.round((a.confidence || 0) * 100)}%</strong><small>confidence = evidence strength, not profit odds</small>`],
    ["Analyst split", analystSplit(a)],
    ["Price", a.price != null
      ? `<strong>$${Number(a.price).toFixed(2)}</strong><small>as of ${escapeHtml(formatDateTime(a.as_of))}</small>`
      : '<span class="muted">Unavailable</span>'],
    ["5-day forecast", a.forecast_price_5d != null
      ? `<strong>$${Number(a.forecast_price_5d).toFixed(2)} (${Number(a.forecast_change_5d_pct) >= 0 ? "+" : ""}${Number(a.forecast_change_5d_pct || 0).toFixed(2)}%)</strong><small>estimate, not a price target</small>`
      : '<span class="muted">Unavailable</span>'],
    ["Data age", dataAge(a)],
    ["Risk flags", (a.risk_flags || []).length
      ? `<div class="flag-list">${a.risk_flags.map((f) => `<span class="flag-warn">⚠ ${escapeHtml(f)}</span>`).join("")}</div>`
      : '<span class="muted">None triggered</span>'],
    ["Suggested size", a.suggested_size_usd
      ? `<strong>${escapeHtml(fmtUsd(a.suggested_size_usd))}</strong>`
      : '<span class="muted">No position</span>'],
  ] : [["Result", '<span class="muted">Not found in this run.</span>']];

  card.innerHTML = `
    <header class="compare-col-head">
      <strong class="compare-ticker">${escapeHtml(ticker)}</strong>
      <div class="compare-col-btns">
        <button type="button" class="secondary-btn move-btn" data-move="-1" ${index === 0 ? "disabled" : ""} aria-label="Move ${escapeAttr(ticker)} left">←</button>
        <button type="button" class="secondary-btn move-btn" data-move="1" ${index === items.length - 1 ? "disabled" : ""} aria-label="Move ${escapeAttr(ticker)} right">→</button>
        <button type="button" class="secondary-btn remove-btn" aria-label="Remove ${escapeAttr(ticker)} from the comparison">Remove</button>
      </div>
    </header>
    <dl class="compare-rows">
      ${facts.map(([label, value]) => `<div class="fact"><dt>${escapeHtml(label)}</dt><dd>${value}</dd></div>`).join("")}
    </dl>`;

  card.querySelector(".remove-btn").addEventListener("click", () => {
    setItems(items.filter((_, i) => i !== index));
  });
  card.querySelectorAll(".move-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const from = index;
      const to = index + Number(button.dataset.move);
      const next = [...items];
      [next[from], next[to]] = [next[to], next[from]];
      setItems(next);
    });
  });
  return card;
}

function analystSplit(a) {
  const keys = ["technical", "fundamental", "news", "sentiment", "forecast"];
  const signals = keys.map((key) => [key, a[key]?.signal]).filter(([, signal]) => signal);
  if (!signals.length) return '<span class="muted">No analyst output</span>';
  const counts = {};
  for (const [, signal] of signals) counts[signal] = (counts[signal] || 0) + 1;
  const tally = Object.entries(counts).map(([name, n]) => `${n} ${name}`).join(" · ");
  return `<strong>${escapeHtml(tally)}</strong><small>${signals
    .map(([key, signal]) => `${escapeHtml(key)} ${escapeHtml(signal)}`).join(" · ")}</small>`;
}

function dataAge(a) {
  const q = a.data_quality || {};
  if (q.age_hours == null) return '<span class="muted">Not recorded</span>';
  const age = q.age_hours < 48 ? `${q.age_hours.toFixed(1)}h` : `${(q.age_hours / 24).toFixed(1)}d`;
  return `<strong>${q.stale ? '<span class="flag-warn">' : ""}${age} old${q.stale ? " · stale</span>" : ""}</strong><small>${escapeHtml(formatDateTime(q.as_of || a.as_of))}</small>`;
}

function decisionBadge(decision) {
  if (!decision) return '<span class="muted">n/a</span>';
  const meta = {
    BUY: { className: "buy", icon: "▲" },
    HOLD: { className: "hold", icon: "■" },
    SELL: { className: "sell", icon: "▼" },
  }[decision] || { className: "hold", icon: "■" };
  return `<span class="decision ${meta.className}"><span class="d-icon" aria-hidden="true">${meta.icon}</span>${escapeHtml(decision)}</span>`;
}

function evidenceLabel(confidence) {
  if (confidence == null) return "n/a";
  const pct = Math.round(confidence * 100);
  if (pct >= 70) return "Strong evidence";
  if (pct >= 50) return "Moderate evidence";
  return "Low evidence";
}
