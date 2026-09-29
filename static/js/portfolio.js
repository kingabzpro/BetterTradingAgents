/* Portfolio page (ROADMAP P2.1): the Alpaca paper account IS the portfolio.
   Account summary, equity curve, open positions, the order lifecycle this app
   placed, and per-order performance vs SPY. Without keys the page is a slim
   connect hint; there is no local demo ledger anymore. */

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value).replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const fmt = (value) => (value == null ? "n/a" : `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const fmtUsd0 = (value) => (value == null ? "n/a" : `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`);

// Filled, canceled, expired, rejected: no longer move, cannot be canceled.
const PAPER_TERMINAL = new Set(["filled", "canceled", "expired", "rejected"]);

let paperPositions = [];

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 4200);
}

function setUnconfigured(detail) {
  $("paper-takeover").classList.add("hidden");
  $("paper-summary").classList.add("hidden");
  for (const id of ["equity-card", "paper-positions-card", "orders-card", "perf-card"]) {
    $(id).classList.add("hidden");
  }
  $("download-csv").classList.add("hidden");
  $("paper-connect-hint").classList.remove("hidden");
  $("paper-status").textContent = detail ? String(detail) : "";
}

async function load() {
  $("paper-status").textContent = "Loading…";
  try {
    const status = await fetch("/api/broker/status").then((r) => (r.ok ? r.json() : null));
    if (!status || !status.configured) {
      setUnconfigured("");
      return;
    }
    const [account, positions, orders] = await Promise.all([
      fetch("/api/broker/account").then((r) => (r.ok ? r.json() : Promise.reject(new Error("account read failed")))),
      fetch("/api/broker/positions").then((r) => (r.ok ? r.json() : [])).catch(() => []),
      fetch("/api/broker/orders").then((r) => (r.ok ? r.json() : [])).catch(() => []),
    ]);
    $("paper-connect-hint").classList.add("hidden");
    $("paper-takeover").classList.remove("hidden");
    $("paper-summary").classList.remove("hidden");
    $("paper-positions-card").classList.remove("hidden");
    $("paper-status").textContent = "";
    $("paper-killswitch").classList.toggle("hidden", Boolean(status.enabled));
    renderSummary(account, positions);
    renderPositions(positions);
    renderOrders(orders);
    renderEquityCurve();
    renderPerformance();
    $("download-csv").classList.toggle("hidden", positions.length === 0);
  } catch (error) {
    setUnconfigured(error.message);
  }
}

function renderSummary(account, positions) {
  const knownValue = positions.reduce((sum, p) => sum + Number(p.market_value || 0), 0);
  $("pc-cash").textContent = fmt(account.cash);
  $("pc-value").textContent = fmt(knownValue);
  $("pc-equity").textContent = fmt(account.equity);
  const lastPnl = account.equity != null && account.last_equity != null
    ? account.equity - account.last_equity
    : null;
  const pnl = $("pc-pnl");
  pnl.textContent = fmt(lastPnl);
  pnl.className = `sc-value ${lastPnl == null ? "" : lastPnl >= 0 ? "green" : "red"}`;
}

function renderPositions(positions) {
  paperPositions = positions;
  const body = $("paper-positions-body");
  body.innerHTML = "";
  $("paper-empty-note").classList.toggle("hidden", positions.length > 0);
  for (const position of positions) {
    const pnlClass = position.unrealized_pl == null ? "" : position.unrealized_pl >= 0 ? "pnl-green" : "pnl-red";
    const row = document.createElement("tr");
    row.innerHTML = `
      <td data-label="Ticker"><strong>${esc(position.symbol)}</strong></td>
      <td class="num" data-label="Qty">${position.quantity % 1 === 0 ? position.quantity : position.quantity.toFixed(4)}</td>
      <td class="num" data-label="Avg entry">${fmt(position.avg_entry_price)}</td>
      <td class="num" data-label="Current">${fmt(position.current_price)}</td>
      <td class="num" data-label="Value">${fmt(position.market_value)}</td>
      <td class="num ${pnlClass}" data-label="Unrealized P&amp;L">${fmt(position.unrealized_pl)}</td>
      <td class="num ${pnlClass}" data-label="P&amp;L %">${position.unrealized_plpc == null ? "n/a" : `${position.unrealized_plpc >= 0 ? "+" : ""}${(Number(position.unrealized_plpc) * 100).toFixed(2)}%`}</td>`;
    body.appendChild(row);
  }
}

/* Download the paper positions as CSV (P2.1): raw values, one row per holding. */
function downloadCsv() {
  const lines = [
    ["Ticker", "Quantity", "Avg entry price", "Current price", "Value", "Unrealized P&L", "P&L %"],
    ...paperPositions.map((p) => [
      p.symbol,
      p.quantity,
      p.avg_entry_price,
      p.current_price == null ? "" : p.current_price,
      p.market_value == null ? "" : p.market_value,
      p.unrealized_pl == null ? "" : p.unrealized_pl,
      p.unrealized_plpc == null ? "" : (Number(p.unrealized_plpc) * 100).toFixed(2),
    ]),
  ];
  const csv = lines.map((line) => line.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "bta-paper-portfolio.csv";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csvCell(value) {
  const text = value == null ? "" : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function statusChip(status) {
  const kind = status === "filled"
    ? "ok"
    : status === "rejected"
      ? "bad"
      : PAPER_TERMINAL.has(status)
        ? "muted"
        : "live";
  return `<span class="status-chip ${kind}">${esc(status)}</span>`;
}

function renderOrders(orders) {
  const body = $("orders-body");
  body.innerHTML = "";
  $("orders-card").classList.toggle("hidden", orders.length === 0);
  $("orders-empty").classList.toggle("hidden", orders.length > 0);
  for (const order of orders) {
    const terminal = PAPER_TERMINAL.has(order.status);
    const filled = order.filled_avg_price != null ? fmt(order.filled_avg_price) : "n/a";
    const decision = order.run_id
      ? `<a href="/?run=${encodeURIComponent(order.run_id)}">View decision</a>`
      : "n/a";
    const row = document.createElement("tr");
    row.innerHTML = `
      <td data-label="Placed">${esc(order.created_at || "")}</td>
      <td data-label="Ticker"><strong>${esc(order.ticker)}</strong></td>
      <td data-label="Side"><span class="side-chip ${esc(order.side)}">${esc(order.side)}</span></td>
      <td class="num" data-label="Notional">${fmtUsd0(order.notional)}</td>
      <td data-label="Status">${statusChip(order.status)}${order.error ? `<small class="order-error">${esc(order.error)}</small>` : ""}</td>
      <td class="num" data-label="Filled avg price">${filled}</td>
      <td data-label="Decision">${decision}</td>
      <td data-label="Action">${terminal || order.error ? "" : `<button class="cancel-btn" type="button" data-cancel="${esc(order.client_order_id)}" data-ticker="${esc(order.ticker)}">Cancel</button>`}</td>`;
    body.appendChild(row);
  }
  body.querySelectorAll("[data-cancel]").forEach((button) => {
    button.addEventListener("click", () => cancelOrder(button.dataset.cancel, button.dataset.ticker));
  });
}

async function cancelOrder(clientOrderId, ticker) {
  if (!window.confirm(`Cancel the pending ${ticker} order?`)) return;
  try {
    const response = await fetch(`/api/broker/orders/${encodeURIComponent(clientOrderId)}`, { method: "DELETE" });
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error(body?.detail || `failed (${response.status})`);
    showToast(`${ticker} order ${body.status}`);
    load();
  } catch (error) {
    showToast(`Could not cancel: ${error.message}`, true);
  }
}

/* Small inline SVG polyline; every mark is also stated as text below the chart. */
async function renderEquityCurve() {
  const card = $("equity-card");
  try {
    const data = await fetch("/api/broker/equity?period=1M")
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null);
    const values = data && data.equity ? data.equity : [];
    if (values.length < 2) {
      card.classList.add("hidden");
      return;
    }
    card.classList.remove("hidden");
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const W = 300;
    const H = 100;
    const y = (value) => H - ((value - min) / span) * (H - 8) - 4;
    const points = values.map((value, i) =>
      `${((i / (values.length - 1)) * W).toFixed(1)},${y(value).toFixed(1)}`
    );
    const first = values[0];
    const last = values[values.length - 1];
    $("equity-chart").innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" ` +
      `aria-label="Paper account equity over the last month; every value is listed below as text">` +
      `<line class="baseline" x1="0" x2="${W}" y1="${y(first).toFixed(1)}" y2="${y(first).toFixed(1)}"></line>` +
      `<polyline class="poly" points="${points.join(" ")}"></polyline></svg>`;
    const changePct = ((last / first - 1) * 100).toFixed(2);
    $("equity-note").textContent =
      `${data.dates[0]} ${fmt(first)} to ${data.dates[data.dates.length - 1]} ${fmt(last)} ` +
      `(${changePct >= 0 ? "+" : ""}${changePct}%)`;
    $("equity-tbody").innerHTML = data.dates
      .map((day, i) => `<tr><td>${esc(day)}</td><td class="num">${fmt(values[i])}</td></tr>`)
      .join("");
  } catch (_) {
    card.classList.add("hidden");
  }
}

/* Per filled order: return since fill and alpha vs SPY over the same window. */
async function renderPerformance() {
  const card = $("perf-card");
  try {
    const rows = await fetch("/api/broker/performance")
      .then((response) => (response.ok ? response.json() : []))
      .catch(() => []);
    const body = $("perf-body");
    body.innerHTML = "";
    card.classList.toggle("hidden", rows.length === 0);
    $("perf-empty").classList.toggle("hidden", rows.length > 0);
    for (const row of rows) {
      const retClass = row.return_pct >= 0 ? "pnl-green" : "pnl-red";
      const alphaText = row.alpha_pct == null
        ? "n/a"
        : `${row.alpha_pct >= 0 ? "+" : ""}${row.alpha_pct}%`;
      const alphaClass = row.alpha_pct == null ? "" : row.alpha_pct >= 0 ? "pnl-green" : "pnl-red";
      const spyText = row.spy_return_pct == null
        ? "n/a"
        : `${row.spy_return_pct >= 0 ? "+" : ""}${row.spy_return_pct}%`;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td data-label="Ticker"><strong>${esc(row.ticker)}</strong></td>
        <td data-label="Placed">${esc(row.placed_at || "")}</td>
        <td class="num" data-label="Fill price">${fmt(row.filled_avg_price)}</td>
        <td class="num" data-label="Now">${fmt(row.current_price)}</td>
        <td class="num ${retClass}" data-label="Return">${row.return_pct >= 0 ? "+" : ""}${row.return_pct}%</td>
        <td class="num" data-label="SPY">${spyText}</td>
        <td class="num ${alphaClass}" data-label="Alpha">${alphaText}</td>`;
      body.appendChild(tr);
    }
  } catch (_) {
    card.classList.add("hidden");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("download-csv").addEventListener("click", downloadCsv);
  load();
});
