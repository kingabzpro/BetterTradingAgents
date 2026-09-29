/* BetterTradingAgents - portfolio page: holdings, paper trades, CSV import. */

const $ = (id) => document.getElementById(id);
const fmt = (value) => (value == null ? "n/a" : `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const fmtUsd0 = (value) => (value == null ? "n/a" : `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
const esc = (value) => String(value).replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const TICKER_PATTERN = /^[A-Z0-9.\-]{1,10}$/;
const TICKER_COLS = ["ticker", "symbol", "stock", "asset"];
const QTY_COLS = ["quantity", "qty", "shares", "units", "amount"];
const PRICE_COLS = ["entry_price", "entry price", "price", "buy price", "purchase price",
  "avg cost", "average cost", "average price", "cost basis", "cost per share", "cost"];

async function loadPortfolio() {
  try {
    const response = await fetch("/api/portfolio");
    if (!response.ok) throw new Error(`failed (${response.status})`);
    const data = await response.json();
    // Paper takeover (P2.1): once an Alpaca paper account is configured it
    // becomes the primary record; a broker read failure keeps the legacy view.
    let paper = null;
    try {
      const status = await fetch("/api/broker/status").then((r) => (r.ok ? r.json() : null));
      if (status && status.configured) {
        const [account, positions, orders] = await Promise.all([
          fetch("/api/broker/account").then((r) => (r.ok ? r.json() : null)),
          fetch("/api/broker/positions").then((r) => (r.ok ? r.json() : [])),
          fetch("/api/broker/orders").then((r) => (r.ok ? r.json() : [])).catch(() => []),
        ]);
        if (account) paper = { status, account, positions: positions || [], orders: orders || [] };
      }
    } catch (_) {
      paper = null;
    }
    paperMode = Boolean(paper);
    render(data, paper);
  } catch (error) {
    showToast(`Could not load portfolio: ${error.message}`, true);
  }
}

let lastData = null;
let paperMode = false;
// Position search + sort state, mirrored to the URL (ROADMAP P1.6). Filtering
// and sorting never touch the server. Each sort key has a natural default
// direction that matches its option label ("Ticker (A to Z)", etc.).
const posState = { q: "", sort: "value", dir: "desc" };
const DEFAULT_DIR = { value: "desc", return: "desc", ticker: "asc", age: "asc" };

const DEMO_LABELS = {
  "sc-cash-card": "Cash remaining",
  "sc-value-card": "Positions value",
  "sc-equity-card": "Total equity",
  "sc-pnl-card": "Total P&L",
};

function setLabel(cardId, text) {
  const card = $(cardId);
  if (card) card.querySelector(".sc-label").textContent = text;
}

function renderDemoSummary(data) {
  $("paper-connect-hint").classList.remove("hidden");
  $("paper-takeover").classList.add("hidden");
  $("paper-positions-card").classList.add("hidden");
  $("equity-card").classList.add("hidden");
  $("orders-card").classList.add("hidden");
  $("perf-card").classList.add("hidden");
  $("backup-heading").classList.add("hidden");
  $("backup-ledger").classList.remove("backup-mode");
  $("sc-start-card").classList.remove("hidden");
  $("sc-realized-card").classList.remove("hidden");
  for (const [id, label] of Object.entries(DEMO_LABELS)) setLabel(id, label);
  $("sc-start").textContent = fmt(data.starting_cash);
  $("sc-cash").textContent = fmt(data.cash);
  $("sc-value").textContent = fmt(data.positions_value);
  $("sc-equity").textContent = fmt(data.total_equity);
  const pnl = $("sc-pnl");
  pnl.textContent = fmt(data.total_pnl);
  pnl.className = `sc-value ${data.total_pnl == null ? "" : data.total_pnl >= 0 ? "green" : "red"}`;
  const realized = $("sc-realized");
  realized.textContent = fmt(data.realized_pnl);
  realized.className = `sc-value ${data.realized_pnl == null ? "" : data.realized_pnl >= 0 ? "green" : "red"}`;
  const note = $("unpriced-note");
  note.textContent = data.unpriced_count
    ? `Live price unavailable for ${data.unpriced_count} position${data.unpriced_count === 1 ? "" : "s"}; totals exclude ${data.unpriced_count === 1 ? "it" : "them"}.`
    : "";
  note.classList.toggle("hidden", !data.unpriced_count);
}

// Paper numbers take over the summary cards; the demo-only cards drop out.
function renderPaperSummary(paper) {
  $("paper-connect-hint").classList.add("hidden");
  $("paper-takeover").classList.remove("hidden");
  $("paper-positions-card").classList.remove("hidden");
  $("backup-heading").classList.remove("hidden");
  $("backup-ledger").classList.add("backup-mode");
  $("sc-start-card").classList.add("hidden");
  $("sc-realized-card").classList.add("hidden");
  setLabel("sc-cash-card", "Cash (paper)");
  setLabel("sc-value-card", "Positions value (paper)");
  setLabel("sc-equity-card", "Equity (paper)");
  setLabel("sc-pnl-card", "Last P&L (paper)");
  const { account, positions, status } = paper;
  $("paper-killswitch").classList.toggle("hidden", Boolean(status.enabled));
  $("sc-cash").textContent = fmt(account.cash);
  const knownValue = positions.reduce((sum, position) => sum + Number(position.market_value || 0), 0);
  $("sc-value").textContent = fmt(knownValue);
  $("sc-equity").textContent = fmt(account.equity);
  const lastPnl = account.equity != null && account.last_equity != null
    ? account.equity - account.last_equity
    : null;
  const pnl = $("sc-pnl");
  pnl.textContent = fmt(lastPnl);
  pnl.className = `sc-value ${lastPnl == null ? "" : lastPnl >= 0 ? "green" : "red"}`;
  $("unpriced-note").classList.add("hidden");
  $("replay-all-btn").classList.toggle("hidden", !paper.status.enabled);
  renderPaperPositions(positions);
  renderPaperOrders(paper.orders || []);
  renderEquityCurve();
  renderPerformance();
}

function renderPaperPositions(positions) {
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

/* ---------- paper order lifecycle, equity curve, performance (P2.1) ---------- */

// Filled, canceled, expired, rejected: no longer move, cannot be canceled.
const PAPER_TERMINAL = new Set(["filled", "canceled", "expired", "rejected"]);

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

function renderPaperOrders(orders) {
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
    button.addEventListener("click", () => cancelPaperOrder(button.dataset.cancel, button.dataset.ticker));
  });
}

async function cancelPaperOrder(clientOrderId, ticker) {
  if (!window.confirm(`Cancel the pending ${ticker} order?`)) return;
  try {
    const response = await fetch(`/api/broker/orders/${encodeURIComponent(clientOrderId)}`, { method: "DELETE" });
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error(body?.detail || `failed (${response.status})`);
    showToast(`${ticker} order ${body.status}`);
    loadPortfolio();
  } catch (error) {
    showToast(`Could not cancel: ${error.message}`, true);
  }
}

// Small inline SVG polyline; every mark is also stated as text below the chart.
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

// Per filled order: return since fill and alpha vs SPY over the same window.
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

function render(data, paper = null) {
  lastData = data;
  if (paper) renderPaperSummary(paper);
  else renderDemoSummary(data);
  renderPositions();
}

function visiblePositions() {
  const positions = lastData ? lastData.positions : [];
  const q = posState.q.trim().toUpperCase();
  const rows = positions.filter((position) => !q || position.ticker.includes(q));
  const dir = posState.dir === "asc" ? 1 : -1;
  const num = (value) => (value == null ? -Infinity : Number(value));
  const ageMs = (position) => {
    const time = new Date(position.added_at || "").getTime();
    return Number.isNaN(time) ? -Infinity : time;
  };
  const key = {
    value: (position) => num(position.value),
    return: (position) => num(position.pnl_pct),
    ticker: (position) => position.ticker,
    age: ageMs,
  }[posState.sort] || (() => 0);
  rows.sort((a, b) => {
    const ka = key(a);
    const kb = key(b);
    return typeof ka === "string" ? dir * ka.localeCompare(kb) : dir * (ka - kb);
  });
  return rows;
}

function renderPositions() {
  const rows = visiblePositions();
  const total = lastData ? lastData.positions.length : 0;
  $("empty-note").classList.toggle("hidden", total > 0);
  $("pos-no-match").classList.toggle("hidden", total === 0 || rows.length > 0);

  const body = $("positions-body");
  body.innerHTML = "";
  for (const position of rows) {
    const pnlClass = position.pnl == null ? "" : position.pnl >= 0 ? "pnl-green" : "pnl-red";
    const tracked = position.external
      ? '<span class="src-tag" title="Tracked holding: added manually or imported; does not use demo cash">tracked</span>'
      : "";
    // Replay actions only exist while a paper account is the primary view.
    const replay = paperMode && !position.replay_client_order_id
      ? `<button class="replay-btn" type="button" title="Place a paper BUY at this position's current live value" data-replay-id="${position.id}" data-replay-ticker="${esc(position.ticker)}">Replay</button>`
      : "";
    const replayed = position.replay_client_order_id
      ? '<span class="src-tag" title="Replayed into the Alpaca paper account">replayed</span>'
      : "";
    const row = document.createElement("tr");
    row.innerHTML = `
      <td data-label="Ticker"><strong>${esc(position.ticker)}</strong>${tracked}${replayed}</td>
      <td class="num" data-label="Quantity">${position.quantity % 1 === 0 ? position.quantity : position.quantity.toFixed(4)}</td>
      <td class="num" data-label="Entry">${fmt(position.entry_price)}</td>
      <td class="num" data-label="Current">${fmt(position.current_price)}</td>
      <td class="num" data-label="Cost">${fmt(position.cost)}</td>
      <td class="num" data-label="Value">${fmt(position.value)}</td>
      <td class="num ${pnlClass}" data-label="P&amp;L">${fmt(position.pnl)}</td>
      <td class="num ${pnlClass}" data-label="P&amp;L %">${position.pnl_pct == null ? "n/a" : `${position.pnl_pct >= 0 ? "+" : ""}${position.pnl_pct}%`}</td>
      <td class="muted col-added" data-label="Added">${esc(position.added_at || "")}</td>
      <td data-label="Action">${replay}<button class="close-btn" type="button" title="Close the entire position at the live price" data-close-id="${position.id}" data-close-ticker="${esc(position.ticker)}">Close</button></td>`;
    body.appendChild(row);
  }
  body.querySelectorAll("[data-close-id]").forEach((button) => {
    button.addEventListener("click", () => closePosition(Number(button.dataset.closeId), button.dataset.closeTicker));
  });
  body.querySelectorAll("[data-replay-id]").forEach((button) => {
    button.addEventListener("click", () => replayPosition(Number(button.dataset.replayId), button.dataset.replayTicker));
  });
  syncPosChips();

  const historyBody = $("history-body");
  historyBody.innerHTML = "";
  $("history-card").classList.toggle("hidden", (lastData?.history || []).length === 0);
  for (const trade of lastData?.history || []) {
    const pnlClass = trade.pnl == null ? "" : trade.pnl >= 0 ? "pnl-green" : "pnl-red";
    const row = document.createElement("tr");
    row.innerHTML = `
      <td data-label="Ticker"><strong>${esc(trade.ticker)}</strong></td>
      <td class="num" data-label="Quantity">${trade.quantity % 1 === 0 ? trade.quantity : trade.quantity.toFixed(4)}</td>
      <td class="num" data-label="Entry">${fmt(trade.entry_price)}</td>
      <td class="num" data-label="Exit">${fmt(trade.exit_price)}</td>
      <td class="num" data-label="Cost">${fmt(trade.cost)}</td>
      <td class="num" data-label="Proceeds">${fmt(trade.value)}</td>
      <td class="num ${pnlClass}" data-label="Realized P&amp;L">${fmt(trade.pnl)}</td>
      <td class="num ${pnlClass}" data-label="P&amp;L %">${trade.pnl_pct == null ? "n/a" : `${trade.pnl_pct >= 0 ? "+" : ""}${trade.pnl_pct}%`}</td>
      <td class="muted col-added" data-label="Closed">${esc(trade.closed_at || "")}</td>`;
    historyBody.appendChild(row);
  }
}

async function closePosition(id, ticker) {
  if (!window.confirm(`Close the entire ${ticker} position at the current market price?`)) return;
  try {
    const response = await fetch("/api/portfolio/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position_id: id }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      throw new Error(detail?.detail || `failed (${response.status})`);
    }
    const position = await response.json();
    showToast(`${ticker} closed${position.pnl == null ? "" : ` · realized ${position.pnl >= 0 ? "+" : ""}$${position.pnl.toLocaleString()}`}`);
    loadPortfolio();
  } catch (error) {
    showToast(`Could not close ${ticker}: ${error.message}`, true);
  }
}

/* ---------- position search, sort, chips, CSV export (P1.6) ---------- */

function syncPosUrl() {
  const url = new URL(window.location.href);
  if (posState.q.trim()) url.searchParams.set("q", posState.q.trim());
  else url.searchParams.delete("q");
  if (posState.sort !== "value") url.searchParams.set("sort", posState.sort);
  else url.searchParams.delete("sort");
  if (posState.dir !== DEFAULT_DIR[posState.sort]) url.searchParams.set("dir", posState.dir);
  else url.searchParams.delete("dir");
  history.replaceState(null, "", url);
}

function syncPosChips() {
  const has = Boolean(posState.q.trim());
  const box = $("pos-filter-chips");
  box.hidden = !has;
  box.innerHTML = has
    ? `<span class="filter-chip"><span class="filter-chip-label">Search: ${esc(posState.q.trim())}</span><button type="button" class="chip-remove" aria-label="Remove filter Search: ${esc(posState.q.trim())}">&times;<span class="sr-only"> remove</span></button></span>`
    : "";
  $("pos-filter-count").textContent = has ? "1" : "0";
}

function restorePosState() {
  const params = new URLSearchParams(window.location.search);
  posState.q = params.get("q") || "";
  const sort = params.get("sort");
  posState.sort = ["value", "return", "ticker", "age"].includes(sort) ? sort : "value";
  posState.dir = params.get("dir") || DEFAULT_DIR[posState.sort];
  $("pos-search").value = posState.q;
  $("pos-sort").value = posState.sort;
  updateSortDirButton();
  syncPosChips();
}

function updateSortDirButton() {
  const button = $("pos-sort-dir");
  button.textContent = posState.dir === "asc" ? "↑" : "↓";
  button.setAttribute("aria-label", `Sort direction ${posState.dir === "asc" ? "ascending" : "descending"}, activate to reverse`);
}

// CSV of the visible scope only: the rows currently shown after search and
// sort, with raw values so the file can be re-imported or charted.
function exportCsv() {
  const rows = visiblePositions();
  const lines = [
    ["Ticker", "Quantity", "Entry price", "Current price", "Cost", "Value", "P&L", "P&L %", "Added"],
    ...rows.map((position) => [
      position.ticker,
      position.quantity,
      position.entry_price == null ? "" : position.entry_price,
      position.current_price == null ? "" : position.current_price,
      position.cost == null ? "" : position.cost,
      position.value == null ? "" : position.value,
      position.pnl == null ? "" : position.pnl,
      position.pnl_pct == null ? "" : position.pnl_pct,
      position.added_at || "",
    ]),
  ];
  const csv = lines.map((line) => line.map(csvCell).join(",")).join("\r\n");
  downloadFile("bta-portfolio-positions.csv", new Blob([csv], { type: "text/csv" }));
}

function csvCell(value) {
  const text = value == null ? "" : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
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

/* ---------- replay into the paper account (P2.1) ---------- */

const REPLAY_DISCLOSURE =
  "A market BUY is placed at the live simulated price, so the paper cost basis will differ from the local entry price. " +
  "Demo cash is not transferable: the paper account keeps its own starting balance, and only open long positions replay.";

async function replayOne(position) {
  try {
    const response = await fetch("/api/broker/replay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position_id: position.id }),
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error(body?.detail || `failed (${response.status})`);
    return `${position.ticker}: replayed (order ${body.status})`;
  } catch (error) {
    return `${position.ticker}: skipped, ${error.message}`;
  }
}

function showReplaySummary(lines) {
  const box = $("replay-summary");
  box.classList.remove("hidden");
  box.innerHTML = `<strong>Replay result</strong><ul>${lines.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>`;
}

async function replayPosition(id, ticker) {
  if (!window.confirm(`Replay the ${ticker} position into the paper account?\n\n${REPLAY_DISCLOSURE}`)) return;
  const row = lastData.positions.find((position) => position.id === id);
  if (!row) return;
  showReplaySummary([await replayOne(row)]);
  loadPortfolio();
}

async function replayAll() {
  const pending = lastData.positions.filter((position) => !position.replay_client_order_id);
  if (!pending.length) {
    showToast("Every open position is already replayed.");
    return;
  }
  if (!window.confirm(`Replay ${pending.length} open position${pending.length === 1 ? "" : "s"} into the paper account?\n\n${REPLAY_DISCLOSURE}`)) return;
  const lines = [];
  for (const row of pending) lines.push(await replayOne(row));
  showReplaySummary(lines);
  loadPortfolio();
}

/* ---------- manual add ---------- */

async function addHolding() {
  const ticker = $("add-ticker").value.trim().toUpperCase();
  const quantity = Number($("add-shares").value);
  const priceText = $("add-price").value.trim();
  const entryPrice = priceText === "" ? null : Number(priceText);
  let problem = null;
  if (!TICKER_PATTERN.test(ticker)) problem = "enter a valid ticker (letters, digits, dot or dash)";
  else if (!Number.isFinite(quantity) || quantity <= 0) problem = "enter a share quantity";
  else if (entryPrice !== null && (!Number.isFinite(entryPrice) || entryPrice <= 0)) problem = "buy price must be a positive number";
  if (problem) { showToast(problem, true); return; }

  const button = $("add-holding-btn");
  button.disabled = true;
  try {
    const response = await fetch("/api/portfolio/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ positions: [{ ticker, quantity, entry_price: entryPrice }] }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || body.imported === 0) throw new Error(body.errors?.[0] || detailText(body) || `failed (${response.status})`);
    showToast(`${quantity} ${ticker} added at ${entryPrice == null ? "the live price" : `$${entryPrice}`}`);
    $("add-ticker").value = ""; $("add-shares").value = ""; $("add-price").value = "";
    loadPortfolio();
  } catch (error) {
    showToast(`Could not add holding: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

function detailText(body) {
  if (typeof body?.detail === "string") return body.detail;
  if (Array.isArray(body?.detail) && body.detail.length) return body.detail[0].msg || JSON.stringify(body.detail[0]);
  return null;
}

/* ---------- CSV import ---------- */

let csvRows = [];

function splitCsvLine(line) {
  return line.split(",").map((cell) => cell.trim().replace(/^"(.*)"$/, "$1"));
}

function parseCsv(text) {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!lines.length) return { error: "The file is empty." };
  const header = splitCsvLine(lines[0]).map((cell) => cell.toLowerCase());
  const findCol = (names) => header.findIndex((cell) => names.includes(cell));
  const t = findCol(TICKER_COLS), q = findCol(QTY_COLS), p = findCol(PRICE_COLS);
  let cols = { t: 0, q: 1, p: 2 };
  let dataLines = lines;
  let startLine = 1;
  if (t !== -1 || q !== -1 || p !== -1) {
    if (t === -1 || q === -1 || p === -1) {
      return { error: "Header row found but a required column is missing: need a ticker (ticker/symbol), a quantity (quantity/shares) and a price (entry_price/price) column." };
    }
    cols = { t, q, p };
    dataLines = lines.slice(1);
    startLine = 2;
  }
  if (!dataLines.length) return { error: "No data rows below the header." };
  if (dataLines.length > 200) return { error: `Too many rows (${dataLines.length}); the maximum is 200 per import.` };
  const rows = dataLines.map((line, index) => {
    const cells = splitCsvLine(line);
    const rawTicker = cells[cols.t] || "";
    const ticker = rawTicker.toUpperCase();
    const quantity = Number(cells[cols.q]);
    const priceText = cells[cols.p];
    const price = priceText === "" || priceText == null ? null : Number(priceText);
    let error = null;
    if (cells.length <= Math.max(cols.t, cols.q, cols.p)) error = "not enough columns";
    else if (!TICKER_PATTERN.test(ticker)) error = "invalid ticker";
    else if (!Number.isFinite(quantity) || quantity <= 0) error = "quantity must be > 0";
    else if (price !== null && (!Number.isFinite(price) || price <= 0)) error = "price must be > 0";
    return { line: startLine + index, ticker, quantity, price, rawTicker, error };
  });
  return { rows };
}

function renderCsvError(message) {
  csvRows = [];
  const box = $("csv-preview");
  box.classList.remove("hidden");
  box.innerHTML = `<div class="preview-title">CSV could not be read</div><p class="preview-error">${esc(message)}</p>`;
}

function renderCsvPreview(result) {
  csvRows = result.rows;
  const ready = csvRows.filter((row) => !row.error);
  const failed = csvRows.filter((row) => row.error);
  const rowsHtml = csvRows.map((row) => row.error
    ? `<tr class="row-error"><td data-label="Ticker">${esc(row.rawTicker) || "n/a"}</td><td class="num" data-label="Quantity">n/a</td><td class="num" data-label="Entry price">n/a</td><td data-label="Status">${esc(row.error)} (line ${row.line})</td></tr>`
    : `<tr><td data-label="Ticker"><strong>${esc(row.ticker)}</strong></td><td class="num" data-label="Quantity">${row.quantity % 1 === 0 ? row.quantity : row.quantity.toFixed(4)}</td><td class="num" data-label="Entry price">${row.price == null ? "live price" : `$${row.price}`}</td><td data-label="Status">ready</td></tr>`
  ).join("");
  const box = $("csv-preview");
  box.classList.remove("hidden");
  box.innerHTML = `
    <div class="preview-title">Preview: ${ready.length} ready, ${failed.length} with errors</div>
    <div class="table-wrap"><table class="preview-table">
      <caption class="sr-only">CSV import preview</caption>
      <thead><tr><th>Ticker</th><th class="num">Quantity</th><th class="num">Entry price</th><th>Status</th></tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table></div>
    <div class="preview-actions">
      <button class="add-btn" id="csv-import-btn" type="button" ${ready.length ? "" : "disabled"}>Import ${ready.length} position${ready.length === 1 ? "" : "s"}</button>
      <button class="secondary-btn" id="csv-cancel-btn" type="button">Cancel</button>
    </div>`;
  $("csv-import-btn").addEventListener("click", confirmCsvImport);
  $("csv-cancel-btn").addEventListener("click", resetCsvImport);
}

async function onCsvChosen(event) {
  const file = event.target.files && event.target.files[0];
  event.target.value = "";
  if (!file) return;
  if (file.size > 256 * 1024) { renderCsvError("File is too large (max 256 KB)."); return; }
  try {
    const text = await file.text();
    const result = parseCsv(text);
    if (result.error) renderCsvError(result.error);
    else renderCsvPreview(result);
  } catch (error) {
    renderCsvError(`Could not read the file: ${error.message}`);
  }
}

async function confirmCsvImport() {
  const payload = csvRows.filter((row) => !row.error)
    .map((row) => ({ ticker: row.ticker, quantity: row.quantity, entry_price: row.price }));
  if (!payload.length) return;
  const button = $("csv-import-btn");
  button.disabled = true;
  try {
    const response = await fetch("/api/portfolio/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ positions: payload }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detailText(body) || `failed (${response.status})`);
    showToast(`Imported ${body.imported} position${body.imported === 1 ? "" : "s"}`);
    if (body.errors && body.errors.length) {
      const box = $("csv-preview");
      box.classList.remove("hidden");
      box.innerHTML = `<div class="preview-title">Imported ${body.imported}, skipped ${body.errors.length}</div>
        <ul class="preview-skip">${body.errors.map((error) => `<li>${esc(error)}</li>`).join("")}</ul>`;
    } else {
      resetCsvImport();
    }
    loadPortfolio();
  } catch (error) {
    showToast(`Could not import: ${error.message}`, true);
    button.disabled = false;
  }
}

function resetCsvImport() {
  csvRows = [];
  const box = $("csv-preview");
  box.classList.add("hidden");
  box.innerHTML = "";
  // The import/cancel buttons just left the DOM; if focus was on them it fell
  // to the page, so return it to the control that opened the preview (P0.3).
  const active = document.activeElement;
  if (!active || active === document.body || box.contains(active)) $("csv-pick-btn").focus();
}

/* ---------- utilities ---------- */

let toastTimer = null;
function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 3200);
}

document.addEventListener("DOMContentLoaded", () => {
  $("add-holding-btn").addEventListener("click", addHolding);
  $("replay-all-btn").addEventListener("click", replayAll);
  $("csv-pick-btn").addEventListener("click", () => $("csv-file").click());
  $("csv-file").addEventListener("change", onCsvChosen);
  restorePosState();
  $("pos-search").addEventListener("input", () => {
    posState.q = $("pos-search").value;
    syncPosUrl();
    renderPositions();
  });
  $("pos-sort").addEventListener("change", () => {
    posState.sort = $("pos-sort").value;
    posState.dir = DEFAULT_DIR[posState.sort];
    updateSortDirButton();
    syncPosUrl();
    renderPositions();
  });
  $("pos-sort-dir").addEventListener("click", () => {
    posState.dir = posState.dir === "asc" ? "desc" : "asc";
    updateSortDirButton();
    syncPosUrl();
    renderPositions();
  });
  $("pos-filter-chips").addEventListener("click", (event) => {
    if (!event.target.closest(".chip-remove")) return;
    posState.q = "";
    $("pos-search").value = "";
    syncPosUrl();
    renderPositions();
    $("pos-search").focus();
  });
  $("export-csv").addEventListener("click", exportCsv);
  const mobile = window.matchMedia("(max-width: 640px)");
  const syncDrawer = () => { if (mobile.matches) $("pos-filter-drawer").open = false; };
  syncDrawer();
  mobile.addEventListener("change", syncDrawer);
  loadPortfolio();
});
