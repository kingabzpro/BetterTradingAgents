/* Paper trading page (ROADMAP P2.1): connection state, account summary,
   positions, and the lifecycle of every order this app placed. The unconfigured
   state is a first-class view, not an error. */

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value).replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const fmt = (value) => (value == null ? "n/a" : `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const fmtUsd0 = (value) => (value == null ? "n/a" : `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`);

// Filled, canceled, expired, rejected: no longer move, cannot be canceled.
const TERMINAL = new Set(["filled", "canceled", "expired", "rejected"]);

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 4200);
}

async function getJson(url) {
  const response = await fetch(url);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : `failed (${response.status})`;
    throw new Error(detail);
  }
  return body;
}

function setUnconfigured(detail) {
  $("live-section").classList.add("hidden");
  $("setup-card").classList.remove("hidden");
  $("test-result").textContent = detail ? String(detail) : "";
  $("trading-status").textContent = "";
}

async function testConnection() {
  const out = $("test-result");
  out.textContent = "Checking…";
  try {
    const account = await getJson("/api/broker/account");
    out.textContent = `Connected: paper account ${account.account_number || ""} is ${account.status}, equity ${fmt(account.equity)}.`;
    load();
  } catch (error) {
    out.textContent = `Not connected: ${error.message}`;
  }
}

async function load() {
  $("trading-status").textContent = "Loading…";
  try {
    const status = await getJson("/api/broker/status");
    if (!status.configured) {
      setUnconfigured("");
      return;
    }
    $("setup-card").classList.add("hidden");
    $("live-section").classList.remove("hidden");
    $("conn-status").textContent = `Connected to the Alpaca paper API${status.enabled ? "" : " (read-only)"}`;
    $("conn-killswitch").classList.toggle("hidden", Boolean(status.enabled));
    const [account, positions, orders] = await Promise.all([
      getJson("/api/broker/account"),
      getJson("/api/broker/positions").catch(() => []),
      getJson("/api/broker/orders").catch(() => []),
    ]);
    renderAccount(account);
    renderPositions(positions);
    renderOrders(orders);
    $("trading-status").textContent = "";
  } catch (error) {
    if (String(error.message).includes("ALPACA_API_KEY_ID")) setUnconfigured(error.message);
    else {
      $("trading-status").textContent = "";
      showToast(`Could not load the trading page: ${error.message}`, true);
    }
  }
}

function renderAccount(account) {
  $("acc-equity").textContent = fmt(account.equity);
  $("acc-cash").textContent = fmt(account.cash);
  $("acc-bp").textContent = fmt(account.buying_power);
  $("acc-last").textContent = fmt(account.last_equity);
}

function renderPositions(positions) {
  const body = $("positions-body");
  body.innerHTML = "";
  $("positions-empty").classList.toggle("hidden", positions.length > 0);
  $("positions-count").textContent = positions.length ? `${positions.length} open` : "";
  for (const position of positions) {
    const pnlClass = position.unrealized_pl == null ? "" : position.unrealized_pl >= 0 ? "pnl-green" : "pnl-red";
    const row = document.createElement("tr");
    row.innerHTML = `
      <td data-label="Ticker"><strong>${esc(position.symbol)}</strong></td>
      <td class="num" data-label="Qty">${Number(position.quantity) % 1 === 0 ? position.quantity : Number(position.quantity).toFixed(4)}</td>
      <td class="num" data-label="Avg entry">${fmt(position.avg_entry_price)}</td>
      <td class="num" data-label="Current">${fmt(position.current_price)}</td>
      <td class="num" data-label="Value">${fmt(position.market_value)}</td>
      <td class="num ${pnlClass}" data-label="Unrealized P&amp;L">${fmt(position.unrealized_pl)}</td>
      <td class="num ${pnlClass}" data-label="P&amp;L %">${position.unrealized_plpc == null ? "n/a" : `${position.unrealized_plpc >= 0 ? "+" : ""}${(Number(position.unrealized_plpc) * 100).toFixed(2)}%`}</td>`;
    body.appendChild(row);
  }
}

function statusChip(status) {
  const kind = status === "filled" ? "ok" : status === "rejected" ? "bad" : TERMINAL.has(status) ? "muted" : "live";
  return `<span class="status-chip ${kind}">${esc(status)}</span>`;
}

function renderOrders(orders) {
  const body = $("orders-body");
  body.innerHTML = "";
  $("orders-empty").classList.toggle("hidden", orders.length > 0);
  for (const order of orders) {
    const terminal = TERMINAL.has(order.status);
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

document.addEventListener("DOMContentLoaded", () => {
  $("test-connection").addEventListener("click", testConnection);
  $("test-connection-2").addEventListener("click", testConnection);
  $("refresh-btn").addEventListener("click", load);
  load();
});
