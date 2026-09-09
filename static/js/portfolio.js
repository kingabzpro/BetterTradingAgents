/* BetterTradingAgents - portfolio page: holdings, paper trades, CSV import. */

const $ = (id) => document.getElementById(id);
const fmt = (value) => (value == null ? "n/a" : `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
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
    render(data);
  } catch (error) {
    showToast(`Could not load portfolio: ${error.message}`, true);
  }
}

function render(data) {
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

  const body = $("positions-body");
  body.innerHTML = "";
  $("empty-note").classList.toggle("hidden", data.positions.length > 0);
  for (const position of data.positions) {
    const pnlClass = position.pnl == null ? "" : position.pnl >= 0 ? "pnl-green" : "pnl-red";
    const tracked = position.external
      ? '<span class="src-tag" title="Tracked holding: added manually or imported; does not use demo cash">tracked</span>'
      : "";
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><strong>${esc(position.ticker)}</strong>${tracked}</td>
      <td class="num">${position.quantity % 1 === 0 ? position.quantity : position.quantity.toFixed(4)}</td>
      <td class="num">${fmt(position.entry_price)}</td>
      <td class="num">${fmt(position.current_price)}</td>
      <td class="num">${fmt(position.cost)}</td>
      <td class="num">${fmt(position.value)}</td>
      <td class="num ${pnlClass}">${fmt(position.pnl)}</td>
      <td class="num ${pnlClass}">${position.pnl_pct == null ? "n/a" : `${position.pnl_pct >= 0 ? "+" : ""}${position.pnl_pct}%`}</td>
      <td class="muted col-added">${esc(position.added_at || "")}</td>
      <td><button class="close-btn" type="button" title="Close the entire position at the live price" data-close-id="${position.id}" data-close-ticker="${esc(position.ticker)}">Close</button></td>`;
    body.appendChild(row);
  }
  body.querySelectorAll("[data-close-id]").forEach((button) => {
    button.addEventListener("click", () => closePosition(Number(button.dataset.closeId), button.dataset.closeTicker));
  });

  const historyBody = $("history-body");
  historyBody.innerHTML = "";
  $("history-card").classList.toggle("hidden", (data.history || []).length === 0);
  for (const trade of data.history || []) {
    const pnlClass = trade.pnl == null ? "" : trade.pnl >= 0 ? "pnl-green" : "pnl-red";
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><strong>${esc(trade.ticker)}</strong></td>
      <td class="num">${trade.quantity % 1 === 0 ? trade.quantity : trade.quantity.toFixed(4)}</td>
      <td class="num">${fmt(trade.entry_price)}</td>
      <td class="num">${fmt(trade.exit_price)}</td>
      <td class="num">${fmt(trade.cost)}</td>
      <td class="num">${fmt(trade.value)}</td>
      <td class="num ${pnlClass}">${fmt(trade.pnl)}</td>
      <td class="num ${pnlClass}">${trade.pnl_pct == null ? "n/a" : `${trade.pnl_pct >= 0 ? "+" : ""}${trade.pnl_pct}%`}</td>
      <td class="muted col-added">${esc(trade.closed_at || "")}</td>`;
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
    ? `<tr class="row-error"><td>${esc(row.rawTicker) || "n/a"}</td><td class="num">n/a</td><td class="num">n/a</td><td>${esc(row.error)} (line ${row.line})</td></tr>`
    : `<tr><td><strong>${esc(row.ticker)}</strong></td><td class="num">${row.quantity % 1 === 0 ? row.quantity : row.quantity.toFixed(4)}</td><td class="num">${row.price == null ? "live price" : `$${row.price}`}</td><td>ready</td></tr>`
  ).join("");
  const box = $("csv-preview");
  box.classList.remove("hidden");
  box.innerHTML = `
    <div class="preview-title">Preview: ${ready.length} ready, ${failed.length} with errors</div>
    <div class="table-wrap"><table class="preview-table">
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
  $("csv-pick-btn").addEventListener("click", () => $("csv-file").click());
  $("csv-file").addEventListener("change", onCsvChosen);
  loadPortfolio();
});
