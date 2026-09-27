/* BetterTradingAgents - durable analysis history page: instant client-side
   search, filters, sort, removable filter chips, and per-run JSON export
   (ROADMAP P1.6). Filtering never touches the server. */

const $ = (id) => document.getElementById(id);
const CLIENT_ID_KEY = "bta:clientId";
const OUTLOOK_LABELS = { day_trade: "Day trading", short_term: "Short term", long_term: "Long term" };
const DEPTH_LABELS = { fast: "Fast", medium: "Medium", expert: "Expert" };

let allRuns = [];
const filters = { q: "", status: "", outlook: "", depth: "", decision: "", from: "", to: "" };
let sortMode = "newest";
// Active filter controls -> URL params so a filtered view survives a refresh
// and can be shared, the same contract as the compare page.
const FILTER_PARAMS = { q: "q", status: "status", outlook: "outlook", depth: "depth", decision: "decision", from: "from", to: "to" };

document.addEventListener("DOMContentLoaded", () => {
  $("refresh-history").addEventListener("click", loadHistory);
  $("clear-history").addEventListener("click", clearHistory);
  $("run-search").addEventListener("input", () => { filters.q = $("run-search").value; applyFilters(); });
  for (const key of ["status", "outlook", "depth", "decision"]) {
    $(`filter-${key}`).addEventListener("change", () => { filters[key] = $("filter-" + key).value; applyFilters(); });
  }
  $("filter-from").addEventListener("change", () => { filters.from = $("filter-from").value; applyFilters(); });
  $("filter-to").addEventListener("change", () => { filters.to = $("filter-to").value; applyFilters(); });
  $("run-sort").addEventListener("change", () => { sortMode = $("run-sort").value; applyFilters(); });
  $("filter-chips").addEventListener("click", onChipClick);
  $("clear-filters-empty").addEventListener("click", clearFilters);
  $("history-list").addEventListener("click", onListClick);
  restoreFromUrl();
  const mobile = window.matchMedia("(max-width: 640px)");
  const syncDrawer = () => { if (mobile.matches) $("filter-drawer").open = false; };
  syncDrawer();
  mobile.addEventListener("change", syncDrawer);
  loadHistory();
});

function readFilters() {
  const params = new URLSearchParams(window.location.search);
  filters.q = params.get("q") || "";
  filters.status = params.get("status") || "";
  filters.outlook = params.get("outlook") || "";
  filters.depth = params.get("depth") || "";
  filters.decision = params.get("decision") || "";
  filters.from = params.get("from") || "";
  filters.to = params.get("to") || "";
  sortMode = params.get("sort") === "oldest" ? "oldest" : "newest";
  $("run-search").value = filters.q;
  for (const key of ["status", "outlook", "depth", "decision"]) $("filter-" + key).value = filters[key];
  $("filter-from").value = filters.from;
  $("filter-to").value = filters.to;
  $("run-sort").value = sortMode;
}

function restoreFromUrl() {
  readFilters();
  syncChips();
  $("filter-count").textContent = String(activeFilters().length);
}

function syncUrl() {
  const url = new URL(window.location.href);
  for (const [key, param] of Object.entries(FILTER_PARAMS)) {
    if (filters[key]) url.searchParams.set(param, filters[key]);
    else url.searchParams.delete(param);
  }
  if (sortMode !== "newest") url.searchParams.set("sort", sortMode);
  else url.searchParams.delete("sort");
  history.replaceState(null, "", url);
}

function activeFilters() {
  const list = [];
  if (filters.q) list.push({ key: "q", label: `Search: ${filters.q.trim()}` });
  if (filters.status) list.push({ key: "status", label: `Status: ${$("filter-status").selectedOptions[0].textContent}` });
  if (filters.outlook) list.push({ key: "outlook", label: `Outlook: ${OUTLOOK_LABELS[filters.outlook] || filters.outlook}` });
  if (filters.depth) list.push({ key: "depth", label: `Depth: ${DEPTH_LABELS[filters.depth] || filters.depth}` });
  if (filters.decision) list.push({ key: "decision", label: filters.decision === "none" ? "No decision" : `Decision: ${filters.decision}` });
  if (filters.from) list.push({ key: "from", label: `From ${filters.from}` });
  if (filters.to) list.push({ key: "to", label: `To ${filters.to}` });
  return list;
}

function syncChips() {
  const active = activeFilters();
  const box = $("filter-chips");
  box.hidden = active.length === 0;
  box.innerHTML = active.map((filter) =>
    `<span class="filter-chip"><span class="filter-chip-label">${escapeHtml(filter.label)}</span>` +
    `<button type="button" class="chip-remove" data-remove-filter="${escapeAttr(filter.key)}" aria-label="Remove filter ${escapeAttr(filter.label)}">×<span class="sr-only"> remove</span></button></span>`
  ).join("") + (active.length > 1 ? '<button id="clear-filters" class="chip-clear" type="button">Clear filters</button>' : "");
  $("filter-count").textContent = String(active.length);
}

function onChipClick(event) {
  const remove = event.target.closest("[data-remove-filter]");
  if (remove) {
    removeFilter(remove.dataset.removeFilter);
    return;
  }
  if (event.target.closest("#clear-filters")) clearFilters();
}

function removeFilter(key) {
  filters[key] = "";
  if (key === "q") $("run-search").value = "";
  else if (key === "from") $("filter-from").value = "";
  else if (key === "to") $("filter-to").value = "";
  else $(`filter-${key}`).value = "";
  applyFilters();
  // Keyboard flow: focus lands on the next chip, or back on search when the
  // last one is gone, so removal never dumps focus to the page body.
  const first = document.querySelector("[data-remove-filter]");
  (first || $("run-search")).focus();
}

function clearFilters() {
  for (const key of Object.keys(filters)) filters[key] = "";
  $("run-search").value = "";
  for (const key of ["status", "outlook", "depth", "decision"]) $(`filter-${key}`).value = "";
  $("filter-from").value = "";
  $("filter-to").value = "";
  applyFilters();
  $("run-search").focus();
}

function visibleRuns() {
  const q = filters.q.trim().toLowerCase();
  const runs = allRuns.filter((run) => {
    if (filters.status && run.status !== filters.status) return false;
    if (filters.outlook && run.outlook !== filters.outlook) return false;
    if (filters.depth && run.depth !== filters.depth) return false;
    const decisions = Object.values(run.decisions || {});
    if (filters.decision === "none" && decisions.length) return false;
    if (filters.decision && filters.decision !== "none" && !decisions.includes(filters.decision)) return false;
    if (filters.from || filters.to) {
      const day = localDay(run.started_at);
      if (filters.from && day < filters.from) return false;
      if (filters.to && day > filters.to) return false;
    }
    if (q) {
      const haystack = [
        run.run_id,
        run.tickers.join(" "),
        Object.entries(run.decisions || {}).map(([ticker, decision]) => `${ticker} ${decision}`).join(" "),
      ].join(" ").toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  });
  runs.sort((a, b) => (sortMode === "oldest" ? a.started_at - b.started_at : b.started_at - a.started_at));
  return runs;
}

function localDay(timestamp) {
  const date = new Date(Number(timestamp) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function applyFilters() {
  syncChips();
  syncUrl();
  renderHistory(visibleRuns());
}

async function loadHistory() {
  $("refresh-history").disabled = true;
  $("history-status").textContent = "Loading run history…";
  try {
    const response = await fetch("/api/runs?limit=100", {
      headers: { "X-Client-ID": getClientId() },
    });
    if (!response.ok) throw new Error(`failed (${response.status})`);
    allRuns = await response.json();
    renderHistory(visibleRuns());
  } catch (error) {
    $("history-status").textContent = "Run history could not be loaded.";
    showToast(`Could not load run history: ${error.message}`);
  } finally {
    $("refresh-history").disabled = false;
  }
}

function renderHistory(runs) {
  const list = $("history-list");
  list.innerHTML = "";
  const noneSaved = allRuns.length === 0;
  const noMatch = !noneSaved && runs.length === 0;
  $("history-empty").classList.toggle("hidden", !noneSaved);
  $("history-no-match").classList.toggle("hidden", !noMatch);
  $("clear-history").disabled = !allRuns.some((run) => run.status !== "running");
  for (const run of runs) list.appendChild(runCard(run));
  const anyFilter = activeFilters().length > 0;
  $("history-status").textContent = noneSaved
    ? "No saved runs"
    : anyFilter
      ? `${runs.length} of ${allRuns.length} saved run${allRuns.length === 1 ? "" : "s"} match the filters`
      : `${allRuns.length} saved run${allRuns.length === 1 ? "" : "s"}, newest first`;
}

async function clearHistory() {
  const confirmed = window.confirm(
    "Delete all completed and interrupted runs from this browser history? This action cannot be undone. Active runs will be kept."
  );
  if (!confirmed) return;
  $("clear-history").disabled = true;
  $("refresh-history").disabled = true;
  try {
    const response = await fetch("/api/runs", {
      method: "DELETE",
      headers: { "X-Client-ID": getClientId() },
    });
    if (!response.ok) throw new Error(`failed (${response.status})`);
    const result = await response.json();
    await loadHistory();
    showToast(`${result.deleted} saved run${result.deleted === 1 ? "" : "s"} deleted`, false);
  } catch (error) {
    $("clear-history").disabled = false;
    showToast(`Could not clear run history: ${error.message}`);
  } finally {
    $("refresh-history").disabled = false;
  }
}

function onListClick(event) {
  const button = event.target.closest("[data-export]");
  if (button) exportRunJson(button.dataset.export, button);
}

// One run as JSON: reuses the existing run-detail response, no new endpoint.
async function exportRunJson(runId, button) {
  button.disabled = true;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    if (!response.ok) throw new Error(`failed (${response.status})`);
    const blob = new Blob([await response.text()], { type: "application/json" });
    downloadFile(`bta-run-${runId}.json`, blob);
  } catch (error) {
    showToast(`Could not download run ${runId}: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

function downloadFile(name, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function runCard(run) {
  const card = document.createElement("article");
  card.className = "history-run";
  const state = runState(run);
  const decisions = run.decisions || {};
  const incomplete = (run.status === "failed" || run.status === "cancelled")
    && run.result_count < run.tickers.length;
  const cost = run.cost_usd != null
    ? `<span>${run.cost_unknown ? "≥" : "≈"} ${fmtCostUsd(run.cost_usd)} model cost</span>`
    : run.cost_unknown
      ? "<span>Model cost unknown</span>"
      : "";
  const rerunHref = `/?rerun=1&tickers=${encodeURIComponent(run.tickers.join(","))}`
    + `&outlook=${encodeURIComponent(run.outlook || "")}`
    + `&depth=${encodeURIComponent(run.depth || "")}`;
  card.innerHTML = `
    <div class="history-run-main">
      <div class="history-run-heading">
        <div>
          <time datetime="${escapeAttr(new Date(run.started_at * 1000).toISOString())}">${escapeHtml(formatDateTime(run.started_at))}</time>
          <span class="history-run-id">Run ${escapeHtml(run.run_id)}</span>
        </div>
        <span class="run-state ${state.className}">${state.icon} ${state.label}</span>
      </div>
      <div class="history-tickers">${run.tickers.map((ticker) => {
        const decision = decisions[ticker];
        const compare = decision
          ? `<a class="history-compare" href="/compare?items=${encodeURIComponent(`${run.run_id}:${ticker}`)}">Compare</a>`
          : "";
        return `<span class="history-ticker"><strong>${escapeHtml(ticker)}</strong>${decision ? decisionBadge(decision) : '<span class="muted">No decision</span>'}${compare}</span>`;
      }).join("")}</div>
      <div class="history-meta"><span>${Number(run.duration_s || 0).toFixed(1)}s</span><span>${run.result_count}/${run.tickers.length} result${run.tickers.length === 1 ? "" : "s"}</span>${run.outlook ? `<span>${escapeHtml(OUTLOOK_LABELS[run.outlook] || run.outlook)} outlook</span>` : ""}${run.depth ? `<span>${escapeHtml(DEPTH_LABELS[run.depth] || run.depth)} depth</span>` : ""}${cost}${run.mock_mode ? "<span>Mock mode</span>" : ""}</div>
      ${incomplete ? `<p class="history-partial">${run.result_count} of ${run.tickers.length} ticker${run.tickers.length === 1 ? "" : "s"} finished before the run was ${run.status === "cancelled" ? "cancelled" : "interrupted"}; the rest have no result.</p>` : ""}
      ${run.error ? `<p class="history-error">${escapeHtml(run.error)}</p>` : ""}
    </div>
    <div class="history-run-actions">
      ${run.status === "running" ? "" : `<a class="history-rerun" href="${escapeAttr(rerunHref)}">Rerun <span aria-hidden="true">↻</span></a>`}
      <a class="history-open" href="/?run=${encodeURIComponent(run.run_id)}">View run <span aria-hidden="true">→</span></a>
      ${run.status === "running" ? "" : `<button class="history-export" type="button" data-export="${escapeAttr(run.run_id)}">Download JSON</button>`}
    </div>`;
  return card;
}

function runState(run) {
  if (run.status === "running") return { className: "running", icon: "●", label: "Running" };
  if (run.status === "cancelled") return { className: "cancelled", icon: "○", label: "Cancelled" };
  if (run.status === "failed") return { className: "failed", icon: "⚠", label: "Interrupted" };
  if (run.has_errors) return { className: "warning", icon: "⚠", label: "Completed with issues" };
  return { className: "complete", icon: "✓", label: "Completed" };
}

function decisionBadge(decision) {
  const meta = {
    BUY: { className: "buy", icon: "▲" },
    HOLD: { className: "hold", icon: "■" },
    SELL: { className: "sell", icon: "▼" },
  }[decision] || { className: "hold", icon: "■" };
  return `<span class="decision ${meta.className}"><span aria-hidden="true">${meta.icon}</span>${escapeHtml(decision)}</span>`;
}

function formatDateTime(timestamp) {
  const date = new Date(Number(timestamp) * 1000);
  if (Number.isNaN(date.getTime())) return "Unknown date";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

// Estimated model cost: per-run totals are cents, so keep more decimals
// until whole dollars make them noise (matches util.js fmtCostUsd).
function fmtCostUsd(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return "$0";
  if (n < 1) return `$${n.toFixed(4)}`;
  if (n < 1000) return `$${n.toFixed(2)}`;
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function showToast(message, isError = true) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.setAttribute("role", isError ? "alert" : "status");
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 4200);
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

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function escapeAttr(text) {
  return escapeHtml(text).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}
