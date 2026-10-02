/* Watchlist page: saved tickers, decision-change rows, analyze selected/all. */

const $ = (id) => document.getElementById(id);
const CLIENT_ID_KEY = "bta:clientId";
const OUTLOOK_LABELS = { day_trade: "Day trading", short_term: "Short term", long_term: "Long term" };
const DEPTH_LABELS = { fast: "Fast", medium: "Medium", expert: "Expert" };
const TICKER_PATTERN = /^[A-Z0-9.\-]{1,10}$/;
const MAX_HINT = { limit: 5 };

document.addEventListener("DOMContentLoaded", () => {
  $("refresh-watchlist").addEventListener("click", loadWatchlist);
  $("clear-watchlist").addEventListener("click", clearWatchlist);
  $("watch-add-btn").addEventListener("click", addManual);
  $("watch-ticker").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addManual();
  });
  $("select-all").addEventListener("change", () => {
    document.querySelectorAll(".watch-item .row-select").forEach((box) => {
      box.checked = $("select-all").checked;
    });
    updateBulkState();
  });
  $("analyze-selected").addEventListener("click", () => analyzeTickers(selectedTickers()));
  $("analyze-all").addEventListener("click", () => analyzeTickers(allTickers()));
  loadHealth();
  loadWatchlist();
});

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) return;
    const health = await response.json();
    MAX_HINT.limit = health.max_tickers || 5;
  } catch (_) { /* default 5 */ }
}

async function loadWatchlist() {
  $("refresh-watchlist").disabled = true;
  $("watch-status").textContent = "Loading watchlist…";
  try {
    const response = await fetch("/api/watchlist", {
      headers: { "X-Client-ID": getClientId() },
    });
    if (!response.ok) throw new Error(`failed (${response.status})`);
    const items = await response.json();
    renderWatchlist(items);
    $("watch-status").textContent = items.length
      ? `${items.length} watched · refresh to recompute changes`
      : "No watched tickers";
  } catch (error) {
    $("watch-status").textContent = "Watchlist could not be loaded.";
    showToast(`Could not load watchlist: ${error.message}`);
  } finally {
    $("refresh-watchlist").disabled = false;
  }
}

function renderWatchlist(items) {
  const list = $("watch-list");
  list.innerHTML = "";
  $("watch-empty").classList.toggle("hidden", items.length > 0);
  $("bulk-card").classList.toggle("hidden", items.length === 0);
  $("clear-watchlist").disabled = items.length === 0;
  $("select-all").checked = false;
  for (const item of items) list.appendChild(watchCard(item));
  updateBulkState();
}

function watchCard(item) {
  const card = document.createElement("article");
  card.className = "watch-item";
  card.dataset.id = String(item.id);
  card.dataset.ticker = item.ticker;
  card.dataset.outlook = item.outlook;
  card.dataset.depth = item.depth;
  const status = changeStatus(item);
  const last = item.last_call;
  const current = item.current_call;
  const priceMove = item.price_move_pct == null
    ? "n/a"
    : `${item.price_move_pct >= 0 ? "+" : ""}${item.price_move_pct}%`;
  const age = item.data_age_hours == null
    ? "unknown"
    : item.data_age_hours < 48
      ? `${item.data_age_hours.toFixed(1)}h old`
      : `${(item.data_age_hours / 24).toFixed(1)}d old`;
  const flags = item.unresolved_risk_flags || [];
  const viewHref = item.view_run_id ? `/?run=${encodeURIComponent(item.view_run_id)}` : "";
  const analyzeHref = `/?rerun=1&tickers=${encodeURIComponent(item.ticker)}`
    + `&outlook=${encodeURIComponent(item.outlook)}`
    + `&depth=${encodeURIComponent(item.depth)}`;

  card.innerHTML = `
    <div class="watch-main">
      <div class="watch-heading">
        <label class="row-select-wrap">
          <input type="checkbox" class="row-select" aria-label="Select ${escapeHtml(item.ticker)}">
        </label>
        <div>
          <strong class="watch-ticker">${escapeHtml(item.ticker)}</strong>
          <span class="muted">${escapeHtml(OUTLOOK_LABELS[item.outlook] || item.outlook)} · ${escapeHtml(DEPTH_LABELS[item.depth] || item.depth)}</span>
        </div>
        <span class="change-badge ${status.className}" title="${escapeAttr(status.title)}">${status.icon} ${escapeHtml(status.label)}</span>
      </div>
      <div class="watch-calls">
        <div class="call-cell">
          <span class="call-label">Last call</span>
          ${last ? decisionBadge(last.decision) : '<span class="muted">n/a</span>'}
          ${last?.confidence != null ? `<small>${evidenceLabel(last.confidence)} · ${Math.round(last.confidence * 100)}%</small>` : ""}
          ${last?.price != null ? `<small>@ $${Number(last.price).toFixed(2)}</small>` : ""}
        </div>
        <div class="call-cell">
          <span class="call-label">Current call</span>
          ${current ? decisionBadge(current.decision) : '<span class="muted">Not reanalyzed</span>'}
          ${current?.confidence != null ? `<small>${evidenceLabel(current.confidence)} · ${Math.round(current.confidence * 100)}%</small>` : ""}
          ${current?.price != null ? `<small>@ $${Number(current.price).toFixed(2)}</small>` : ""}
        </div>
        <div class="call-cell">
          <span class="call-label">Since baseline</span>
          <strong class="${item.price_move_pct == null ? "" : item.price_move_pct >= 0 ? "pos" : "neg"}">${priceMove}</strong>
          <small>live ${item.live_price != null ? `$${Number(item.live_price).toFixed(2)}` : "n/a"}</small>
        </div>
        <div class="call-cell">
          <span class="call-label">Data age</span>
          <strong>${escapeHtml(age)}</strong>
          <small>${current?.as_of || last?.as_of ? escapeHtml(formatStamp(current?.as_of || last?.as_of)) : "no timestamp"}</small>
        </div>
      </div>
      ${item.change_status === "not_reanalyzed" && last
        ? '<p class="watch-note muted">Not reanalyzed since this baseline was saved.</p>'
        : ""}
      ${item.decision_changed
        ? `<p class="watch-note change-line">Decision changed: ${escapeHtml(last?.decision || "?")} → ${escapeHtml(current?.decision || "?")}</p>`
        : ""}
      ${item.evidence_changed
        ? `<p class="watch-note change-line">Evidence strength changed: ${escapeHtml(evidenceLabel(last?.confidence))} → ${escapeHtml(evidenceLabel(current?.confidence))}</p>`
        : ""}
      ${item.note ? `<p class="watch-note">Note: ${escapeHtml(item.note)}</p>` : ""}
      ${flags.length
        ? `<div class="watch-flags"><strong>Unresolved risk</strong>${flags.map((flag) => `<span>⚠ ${escapeHtml(flag)}</span>`).join("")}</div>`
        : ""}
      <div class="watch-meta">
        <span>Saved ${escapeHtml(item.added_at || "")}</span>
        <span>${escapeHtml(OUTLOOK_LABELS[item.outlook] || item.outlook)} · ${escapeHtml(DEPTH_LABELS[item.depth] || item.depth)}</span>
      </div>
    </div>
    <div class="watch-actions-col">
      ${viewHref ? `<a class="history-open" href="${escapeAttr(viewHref)}">View run →</a>` : ""}
      <a class="history-rerun" href="${escapeAttr(analyzeHref)}">Analyze ↻</a>
      <button type="button" class="secondary-btn remove-watch" data-remove="${item.id}">Remove</button>
    </div>`;

  card.querySelector(".remove-watch").addEventListener("click", () => removeItem(item.id, item.ticker));
  card.querySelector(".row-select").addEventListener("change", updateBulkState);
  return card;
}

function changeStatus(item) {
  if (item.change_status === "changed") {
    return { className: "changed", icon: "●", label: "Changed", title: "Decision or evidence strength differs from the baseline call" };
  }
  if (item.change_status === "no_change") {
    return { className: "same", icon: "✓", label: "No change", title: "Reanalyzed; same decision and evidence bucket as the baseline" };
  }
  return { className: "pending", icon: "○", label: "Not reanalyzed", title: "No newer completed analysis since the baseline was saved" };
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

function selectedTickers() {
  return [...document.querySelectorAll(".watch-item")]
    .filter((card) => card.querySelector(".row-select")?.checked)
    .map((card) => card.dataset.ticker);
}

function allTickers() {
  return [...document.querySelectorAll(".watch-item")].map((card) => card.dataset.ticker);
}

function updateBulkState() {
  const total = document.querySelectorAll(".watch-item").length;
  const selected = selectedTickers().length;
  const limit = MAX_HINT.limit;
  $("analyze-selected").disabled = selected === 0 || selected > limit;
  $("analyze-all").disabled = total === 0 || total > limit;
  $("bulk-hint").textContent = total > limit
    ? `Max ${limit} tickers per run: select up to ${limit}, or clear some watched items.`
    : selected > limit
      ? `Select at most ${limit} tickers.`
      : selected
        ? `${selected} selected · up to ${limit} per run`
        : `Up to ${limit} tickers per run.`;
}

function analyzeTickers(tickers) {
  if (!tickers.length) {
    showToast("Select at least one ticker", true);
    return;
  }
  if (tickers.length > MAX_HINT.limit) {
    showToast(`Max ${MAX_HINT.limit} tickers per analysis`, true);
    return;
  }
  const chosen = [...document.querySelectorAll(".watch-item")].filter(
    (card) => tickers.includes(card.dataset.ticker)
  );
  const outlook = chosen[0]?.dataset.outlook || "short_term";
  const depth = chosen[0]?.dataset.depth || "pro";
  window.location.href =
    `/?rerun=1&tickers=${encodeURIComponent(tickers.join(","))}` +
    `&outlook=${encodeURIComponent(outlook)}` +
    `&depth=${encodeURIComponent(depth)}`;
}

async function addManual() {
  const ticker = $("watch-ticker").value.trim().toUpperCase();
  const note = $("watch-note").value.trim();
  if (!TICKER_PATTERN.test(ticker)) {
    showToast("Enter a valid ticker (letters, digits, dot or dash)", true);
    return;
  }
  const button = $("watch-add-btn");
  button.disabled = true;
  try {
    const response = await fetch("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-ID": getClientId() },
      body: JSON.stringify({ ticker, note }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `failed (${response.status})`);
    showToast(body.already_watched ? `${ticker} was already watched; note updated` : `${ticker} added to watchlist`);
    $("watch-ticker").value = "";
    $("watch-note").value = "";
    await loadWatchlist();
  } catch (error) {
    showToast(`Could not add ${ticker}: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function removeItem(id, ticker) {
  if (!window.confirm(`Remove ${ticker} from the watchlist?`)) return;
  try {
    const response = await fetch(`/api/watchlist/${id}`, {
      method: "DELETE",
      headers: { "X-Client-ID": getClientId() },
    });
    if (!response.ok) throw new Error(`failed (${response.status})`);
    showToast(`${ticker} removed`);
    await loadWatchlist();
  } catch (error) {
    showToast(`Could not remove ${ticker}: ${error.message}`);
  }
}

async function clearWatchlist() {
  if (!window.confirm("Remove every ticker from this watchlist?")) return;
  try {
    const response = await fetch("/api/watchlist", {
      method: "DELETE",
      headers: { "X-Client-ID": getClientId() },
    });
    if (!response.ok) throw new Error(`failed (${response.status})`);
    const result = await response.json();
    showToast(`${result.deleted} ticker${result.deleted === 1 ? "" : "s"} removed`, false);
    await loadWatchlist();
  } catch (error) {
    showToast(`Could not clear watchlist: ${error.message}`);
  }
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

function formatStamp(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function escapeAttr(text) {
  return escapeHtml(text).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function showToast(message, isError = true) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.setAttribute("role", isError ? "alert" : "status");
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 4200);
}
