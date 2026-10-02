/* Paper order review and placement (ROADMAP P2.1). One shared broker-status
   fetch per page load; the inline review step shows what a person needs to
   check before committing, and the server re-validates everything anyway. */

import { $, escapeAttr, escapeHtml, fmtUsd, formatDateTime } from "./util.js?v=11";

let infoPromise = null;
let info = { status: null, positions: [] };

// Status plus open positions, fetched once per page and reused by every card.
export function brokerInfo() {
  if (!infoPromise) {
    infoPromise = (async () => {
      const status = await fetch("/api/broker/status")
        .then((response) => (response.ok ? response.json() : null))
        .catch(() => null);
      const positions = status?.configured
        ? await fetch("/api/broker/positions")
            .then((response) => (response.ok ? response.json() : []))
            .catch(() => [])
        : [];
      return { status, positions };
    })();
  }
  return infoPromise.then((value) => {
    info = value;
    return value;
  });
}

// A paper order is offered on non-error BUY calls, and on SELL only when the
// paper account actually holds the ticker (no shorting through this feature).
export async function paperOrderAvailable(analysis) {
  if (analysis.error || !["BUY", "SELL"].includes(analysis.decision)) return false;
  const { status, positions } = await brokerInfo();
  if (!status?.configured) return false;
  if (analysis.decision === "SELL") {
    return positions.some((position) => position.symbol === analysis.ticker);
  }
  return true;
}

function notionalFor(analysis, positions) {
  if (analysis.decision === "SELL") {
    const held = positions.find((position) => position.symbol === analysis.ticker);
    if (held?.market_value != null) return Math.max(1, Math.floor(held.market_value));
  }
  return Math.floor(analysis.suggested_size_usd || 10000);
}

// Inline review step appended to the result card's actions area. Order type,
// TIF, and extended hours are fixed server-side; they are stated, not options.
export async function attachPaperOrder(actionsEl, analysis, runId) {
  if (!(await paperOrderAvailable(analysis))) return;
  const ticker = analysis.ticker;
  const { status, positions } = await brokerInfo();
  const notional = notionalFor(analysis, positions);
  const panel = document.createElement("div");
  panel.className = "paper-order";
  panel.innerHTML = `
    <div class="add-row paper-row">
      <button class="add-btn paper-open-btn" id="paper-open-${escapeAttr(ticker)}" type="button"
              aria-expanded="false" aria-controls="paper-review-${escapeAttr(ticker)}">Paper order</button>
      ${status.enabled ? "" : '<span class="muted">Submissions are disabled (kill switch)</span>'}
    </div>
    <div class="paper-review hidden" id="paper-review-${escapeAttr(ticker)}">
      <div class="paper-facts">
        <div><span>Symbol</span><strong>${escapeHtml(ticker)}</strong></div>
        <div><span>Side</span><strong>${escapeHtml(analysis.decision)}</strong></div>
        <div><span>Order</span><strong>Market · good for the day</strong><small>extended hours off</small></div>
        <div><span>Reference price</span><strong>${analysis.price != null ? `$${Number(analysis.price).toFixed(2)}` : "Unavailable"}</strong><small>${analysis.as_of ? escapeHtml(formatDateTime(analysis.as_of)) : "no timestamp"}</small></div>
        <div><span>Buying power</span><strong id="paper-bp-${escapeAttr(ticker)}">loading…</strong></div>
        <div><span>Exposure after</span><strong id="paper-exp-${escapeAttr(ticker)}">loading…</strong></div>
      </div>
      <div class="add-row">
        <label for="paper-notional-${escapeAttr(ticker)}">Notional (USD)</label>
        <input id="paper-notional-${escapeAttr(ticker)}" type="number" min="1" step="1" value="${notional}" ${status.enabled ? "" : "disabled"}>
        <span class="muted">cap ${fmtUsd(status.max_order_usd || 0)}</span>
      </div>
      <label class="paper-confirm">
        <input type="checkbox" id="paper-confirm-${escapeAttr(ticker)}" ${status.enabled ? "" : "disabled"}>
        <span>I reviewed this order (symbol, side, size)</span>
      </label>
      <div class="add-row">
        <button class="add-btn" id="paper-place-${escapeAttr(ticker)}" type="button" disabled>Place paper order</button>
        <span class="muted">Simulated fills; not live-trading proof</span>
      </div>
      <p class="paper-status hidden" id="paper-status-${escapeAttr(ticker)}" role="status" aria-live="polite"></p>
    </div>`;
  // Primary position while connected: the decision this card is about becomes
  // a paper order first; the demo-portfolio add keeps its explicit demo label.
  actionsEl.prepend(panel);

  const openBtn = $(`paper-open-${ticker}`);
  const reviewEl = $(`paper-review-${ticker}`);
  openBtn.addEventListener("click", () => {
    const show = reviewEl.classList.contains("hidden");
    reviewEl.classList.toggle("hidden", !show);
    openBtn.setAttribute("aria-expanded", String(show));
    if (show) loadAccountFacts(ticker, analysis);
  });

  const confirmBox = $(`paper-confirm-${ticker}`);
  const placeBtn = $(`paper-place-${ticker}`);
  confirmBox.addEventListener("change", () => {
    placeBtn.disabled = !confirmBox.checked;
  });
  placeBtn.addEventListener("click", () => placePaperOrder(ticker, analysis, runId));
}

async function loadAccountFacts(ticker, analysis) {
  const account = await fetch("/api/broker/account")
    .then((response) => (response.ok ? response.json() : null))
    .catch(() => null);
  const bpEl = $(`paper-bp-${ticker}`);
  const expEl = $(`paper-exp-${ticker}`);
  if (!account || bpEl == null) return;
  bpEl.textContent = account.buying_power != null ? fmtUsd(account.buying_power) : "Unavailable";
  let exposure = "Unavailable";
  if (account.equity != null && account.cash != null && account.equity > 0) {
    const notional = Number($(`paper-notional-${ticker}`)?.value || 0);
    const after = ((account.equity - account.cash + notional) / account.equity) * 100;
    exposure = `${after.toFixed(1)}% of equity`;
  }
  expEl.textContent = exposure;
}

async function placePaperOrder(ticker, analysis, runId) {
  const statusEl = $(`paper-status-${ticker}`);
  const placeBtn = $(`paper-place-${ticker}`);
  const notional = Number($(`paper-notional-${ticker}`)?.value || 0);
  statusEl.classList.remove("hidden");
  statusEl.textContent = "Submitting…";
  placeBtn.disabled = true;
  try {
    const response = await fetch("/api/broker/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run_id: runId || "",
        ticker,
        side: analysis.decision === "SELL" ? "sell" : "buy",
        notional,
        confirm: true,
      }),
    });
    const body = await response.json();
    if (!response.ok) {
      statusEl.textContent = `Not placed: ${body.detail || response.statusText}`;
      placeBtn.disabled = false;
      return;
    }
    const filled = body.filled_avg_price != null
      ? ` at $${Number(body.filled_avg_price).toFixed(2)}`
      : "";
    statusEl.innerHTML =
      `Order ${escapeHtml(body.status)}${filled}. ` +
      '<a href="/portfolio">Open your portfolio</a>';
    placeBtn.hidden = true;
  } catch (error) {
    statusEl.textContent = "Submission failed; check the trading page for its final state.";
    placeBtn.disabled = false;
  }
}
