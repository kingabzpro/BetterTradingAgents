/* BetterTradingAgents - durable analysis history page. */

const $ = (id) => document.getElementById(id);
const CLIENT_ID_KEY = "bta:clientId";

document.addEventListener("DOMContentLoaded", () => {
  $("refresh-history").addEventListener("click", loadHistory);
  $("clear-history").addEventListener("click", clearHistory);
  loadHistory();
});

async function loadHistory() {
  $("refresh-history").disabled = true;
  $("history-status").textContent = "Loading run history…";
  try {
    const response = await fetch("/api/runs?limit=50", {
      headers: { "X-Client-ID": getClientId() },
    });
    if (!response.ok) throw new Error(`failed (${response.status})`);
    const runs = await response.json();
    renderHistory(runs);
    $("history-status").textContent = runs.length
      ? `${runs.length} saved run${runs.length === 1 ? "" : "s"}, newest first`
      : "No saved runs";
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
  $("history-empty").classList.toggle("hidden", runs.length > 0);
  $("clear-history").disabled = !runs.some((run) => run.status !== "running");
  for (const run of runs) list.appendChild(runCard(run));
}

async function clearHistory() {
  const confirmed = window.confirm(
    "Delete all completed and interrupted runs from this browser's history? This cannot be undone. Active runs will be kept."
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

function runCard(run) {
  const card = document.createElement("article");
  card.className = "history-run";
  const state = runState(run);
  const decisions = run.decisions || {};
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
        return `<span class="history-ticker"><strong>${escapeHtml(ticker)}</strong>${decision ? decisionBadge(decision) : '<span class="muted">No decision</span>'}</span>`;
      }).join("")}</div>
      <div class="history-meta"><span>${Number(run.duration_s || 0).toFixed(1)}s</span><span>${run.result_count}/${run.tickers.length} result${run.tickers.length === 1 ? "" : "s"}</span>${run.mock_mode ? "<span>Mock mode</span>" : ""}</div>
      ${run.error ? `<p class="history-error">${escapeHtml(run.error)}</p>` : ""}
    </div>
    <a class="history-open" href="/?run=${encodeURIComponent(run.run_id)}">View run <span aria-hidden="true">→</span></a>`;
  return card;
}

function runState(run) {
  if (run.status === "running") return { className: "running", icon: "●", label: "Running" };
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
