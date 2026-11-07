/* Paper order review and placement (ROADMAP P2.1). One shared broker-status
   fetch per page load; the inline review step shows what a person needs to
   check before committing, and the server re-validates everything anyway. */

import { loadPriceChart } from "./price-chart.js";
import { $, escapeAttr, escapeHtml, fmtUsd, formatDateTime } from "./util.js";

let infoPromise = null;
let info = { status: null, positions: [], orders: [] };

// Alpaca accepts at most 6 decimals on share quantities; always round DOWN.
const floor6 = (value) => Math.floor(value * 1e6) / 1e6;
const fmtQty = (value) => String(floor6(Number(value)));

// Status, open positions, and recent orders, fetched once per page and reused
// by every card. The orders list feeds the wash-trade warning below: open
// orders are reconciled server-side before they are returned.
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
      const orders = status?.configured
        ? await fetch("/api/broker/orders?limit=50")
            .then((response) => (response.ok ? response.json() : []))
            .catch(() => [])
        : [];
      return { status, positions, orders };
    })();
  }
  return infoPromise.then((value) => {
    info = value;
    return value;
  });
}

// A paper order is offered on non-error BUY calls with a reference price, and
// on SELL only when the paper account actually holds the ticker (no shorting
// through this feature).
export async function paperOrderAvailable(analysis) {
  if (analysis.error || !["BUY", "SELL"].includes(analysis.decision)) return false;
  if (analysis.decision === "BUY" && analysis.price == null) return false;
  const { status, positions } = await brokerInfo();
  if (!status?.configured) return false;
  if (analysis.decision === "SELL") {
    return positions.some((position) => position.symbol === analysis.ticker);
  }
  return true;
}

function sellableShares(analysis, positions) {
  const held = positions.find((position) => position.symbol === analysis.ticker);
  if (!held) return 0;
  return floor6(held.available != null ? held.available : held.quantity);
}

// Human-readable held amount: sub-cent-of-a-share leftovers from full exits
// are dust, and printing "1.59e-07 sh" tells the reader nothing.
function fmtHeld(position) {
  const qty = Number(position?.quantity || 0);
  if (qty > 0 && qty < 0.01) return "under 0.01 sh (leftover dust from a previous exit)";
  return `${fmtQty(qty)} sh`;
}

// Orders are sized in shares, never dollars: a dollar notional gets converted
// to shares by the broker at its own price, which let a price tick turn a
// full exit into an over-sell. Sells default to the whole sellable position;
// buys convert the suggested USD size at the decision's reference price.
function qtyFor(analysis, positions) {
  if (analysis.decision === "SELL") return sellableShares(analysis, positions);
  return floor6((analysis.suggested_size_usd || 10000) / Number(analysis.price));
}

const TERMINAL_STATUSES = new Set(["filled", "canceled", "cancelled", "expired", "rejected"]);

function workingOrders(ticker, side) {
  return (info.orders || []).filter(
    (order) =>
      order.ticker === ticker &&
      order.side === side &&
      !TERMINAL_STATUSES.has(String(order.status || "").toLowerCase()),
  );
}

// Everything a person should know before committing, in plain language:
// a working opposite-side order (Alpaca rejects the combination as a
// potential wash trade), and on BUYs whether the position already exists
// (autopilot never adds to a position; the manual path may, so it warns).
function orderWarnings(analysis) {
  const ticker = analysis.ticker;
  const warnings = [];
  if (analysis.decision === "SELL") {
    const blocking = workingOrders(ticker, "buy");
    if (blocking.length) {
      const first = blocking[0];
      const size = first.notional != null ? ` for ${fmtUsd(first.notional)}` : "";
      warnings.push(
        `An open buy order${size} (status ${escapeHtml(String(first.status))}) is still working. ` +
          "Alpaca rejects buy and sell orders working in the same symbol as a potential wash trade: " +
          "cancel it on the Trades page or wait for it to fill before selling.",
      );
    }
  } else {
    const held = info.positions.find((position) => position.symbol === ticker);
    if (held) {
      const qty = held.quantity != null ? `${Number(held.quantity).toFixed(2)} shares` : "a position";
      const value = held.market_value != null ? `, ~${fmtUsd(held.market_value)}` : "";
      warnings.push(
        `You already hold ${escapeHtml(ticker)} (${qty}${value}). This order adds to the ` +
          "position; autopilot never adds to a position.",
      );
    }
  }
  return warnings;
}

// Inline review step appended to the result card's actions area. Order type,
// TIF, and extended hours are fixed server-side; they are stated, not options.
export async function attachPaperOrder(actionsEl, analysis, runId) {
  if (!(await paperOrderAvailable(analysis))) return;
  const ticker = analysis.ticker;
  const isSell = analysis.decision === "SELL";
  const { status, positions } = await brokerInfo();
  const held = isSell ? positions.find((position) => position.symbol === ticker) : null;
  const heldQty = isSell ? sellableShares(analysis, positions) : 0;
  const refPrice = Number(analysis.price) || 0;
  const capUsd = status.max_order_usd || 0;

  // A sell only makes sense when the shares are worth placing: dust leftovers
  // and locked-up positions get an explanation instead of an order form.
  const sellableValue = heldQty * Number(held?.current_price || analysis.price || 0);
  if (isSell && (heldQty <= 0 || sellableValue < 1)) {
    let reason;
    if (held && Number(held.quantity) > 0 && held.available != null && Number(held.available) <= 0) {
      reason = "the sellable shares are locked in open orders";
    } else if (heldQty <= 0) {
      reason = "that is below the smallest sellable quantity (a millionth of a share)";
    } else {
      reason = `at the broker's price that is about ${fmtUsd(sellableValue)}, under the $1 minimum order`;
    }
    const note = document.createElement("div");
    note.className = "paper-order";
    note.innerHTML =
      `<p class="hint flag-warn paper-warn">You hold ${escapeHtml(fmtHeld(held))} of ` +
      `${escapeHtml(ticker)}: ${reason}. Nothing to place.</p>`;
    actionsEl.prepend(note);
    return;
  }

  const qty = qtyFor(analysis, positions);
  const warnings = orderWarnings(analysis);
  const sizeHint = isSell
    ? (held && held.available != null && floor6(held.available) !== floor6(held.quantity)
        ? `you hold ${fmtQty(held.quantity)} sh, ${fmtQty(heldQty)} sellable now`
        : `you hold ${fmtQty(held?.quantity ?? heldQty)} sh`)
    : `cap ${fmtUsd(capUsd)} = ${fmtQty(floor6(capUsd / refPrice))} sh`;
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
        <div><span>Live price</span><strong id="paper-live-${escapeAttr(ticker)}">loads with the chart</strong></div>
        <div><span>Buying power</span><strong id="paper-bp-${escapeAttr(ticker)}">loading…</strong></div>
        <div><span>Exposure after</span><strong id="paper-exp-${escapeAttr(ticker)}">loading…</strong></div>
      </div>
      ${warnings.map((text) => `<p class="hint flag-warn paper-warn">${text}</p>`).join("")}
      <div class="chart-holder" id="paper-chart-${escapeAttr(ticker)}"><p class="hint">The six-month price chart loads when the review opens.</p></div>
      <div class="add-row">
        <label for="paper-qty-${escapeAttr(ticker)}">Quantity (shares)</label>
        <input id="paper-qty-${escapeAttr(ticker)}" type="number" min="0.000001" step="any" value="${qty}" ${status.enabled ? "" : "disabled"}>
        ${isSell ? `
        <button type="button" class="add-btn paper-size" data-size="half">Half</button>
        <button type="button" class="add-btn paper-size" data-size="full">Full</button>` : ""}
        <span class="muted">${escapeHtml(sizeHint)}</span>
      </div>
      <label class="paper-confirm">
        <input type="checkbox" id="paper-confirm-${escapeAttr(ticker)}" ${status.enabled ? "" : "disabled"}>
        <span>I reviewed this order (symbol, side, size)</span>
      </label>
      <div class="add-row">
        <button class="add-btn paper-place" id="paper-place-${escapeAttr(ticker)}" type="button" disabled>Place paper order</button>
        <span class="muted">Simulated fills; not live-trading proof</span>
      </div>
      <p class="paper-status hidden" id="paper-status-${escapeAttr(ticker)}" role="status" aria-live="polite"></p>
    </div>`;
  // Primary position while connected: the decision this card is about becomes
  // a paper order first; the demo-portfolio add keeps its explicit demo label.
  actionsEl.prepend(panel);

  // Scope lookups to the panel: the result card can be re-rendered while the
  // broker fetches above were in flight, which detaches this panel from the
  // document and breaks document-wide getElementById lookups.
  const openBtn = panel.querySelector(".paper-open-btn");
  const reviewEl = panel.querySelector(".paper-review");
  const qtyInput = panel.querySelector("input[type=number]");
  const qtyEl = () => panel.querySelector(`[id="paper-qty-${ticker}"]`);

  // Exposure is a function of the typed quantity, so it recomputes whenever
  // the size changes (a preset click or an edit), not just once on open.
  let account = null;
  const updateExposure = () => {
    const expEl = panel.querySelector(`[id="paper-exp-${ticker}"]`);
    if (!expEl) return;
    if (!account || account.equity == null || account.cash == null || account.equity <= 0) {
      expEl.textContent = "Unavailable";
      return;
    }
    const orderUsd = Number(qtyEl()?.value || 0) * refPrice;
    const deployed = account.equity - account.cash;
    // A sell releases exposure instead of adding it.
    const after = deployed + (isSell ? -orderUsd : orderUsd);
    expEl.textContent = `${((after / account.equity) * 100).toFixed(1)}% of equity`;
  };

  openBtn.addEventListener("click", () => {
    const show = reviewEl.classList.contains("hidden");
    reviewEl.classList.toggle("hidden", !show);
    openBtn.setAttribute("aria-expanded", String(show));
    if (show) {
      fetch("/api/broker/account")
        .then((response) => (response.ok ? response.json() : null))
        .catch(() => null)
        .then((data) => {
          account = data;
          const bpEl = panel.querySelector(`[id="paper-bp-${ticker}"]`);
          if (bpEl && account) {
            bpEl.textContent =
              account.buying_power != null ? fmtUsd(account.buying_power) : "Unavailable";
          }
          updateExposure();
        });
      loadMarketContext(ticker, analysis);
    }
  });

  // The sell limit is visible before submit too: typing more than the
  // sellable share count snaps the input back to it.
  qtyInput.addEventListener("change", () => {
    if (isSell && Number(qtyInput.value) > heldQty) {
      qtyInput.value = heldQty;
    }
    updateExposure();
  });
  // Sell sizing presets: half the sellable shares, or all of them; the input
  // itself stays the custom path.
  panel.querySelectorAll(".paper-size").forEach((btn) => {
    btn.addEventListener("click", () => {
      qtyInput.value = fmtQty(btn.dataset.size === "half" ? floor6(heldQty / 2) : heldQty);
      updateExposure();
    });
  });

  const confirmBox = panel.querySelector(".paper-confirm input");
  const placeBtn = panel.querySelector(".paper-place");
  confirmBox.addEventListener("change", () => {
    placeBtn.disabled = !confirmBox.checked;
  });
  placeBtn.addEventListener("click", () => placePaperOrder(ticker, analysis, runId));
}

// The chart and the live price ride one fetch: the review shows the trend and
// the current price next to the decision-time reference before any confirm.
async function loadMarketContext(ticker, analysis) {
  const holder = $(`paper-chart-${ticker}`);
  const liveEl = $(`paper-live-${ticker}`);
  // Not the chart's own dataset.loaded flag: loadPriceChart checks it and
  // would skip rendering when the context fetch marked the holder first.
  if (!holder || holder.dataset.contextLoaded) return;
  holder.dataset.contextLoaded = "1";
  try {
    const data = await fetch(`/api/price-history/${encodeURIComponent(ticker)}`)
      .then((response) => {
        if (!response.ok) throw new Error(`failed (${response.status})`);
        return response.json();
      });
    const dates = data.dates || [];
    const closes = (data.closes || []).map(Number);
    if (liveEl && closes.length) {
      liveEl.textContent =
        `$${closes[closes.length - 1].toFixed(2)}${dates.length ? ` · ${dates[dates.length - 1]}` : ""}`;
    }
    loadPriceChart(`paper-chart-${ticker}`, analysis, data);
  } catch {
    if (liveEl) liveEl.textContent = "Unavailable";
    holder.innerHTML = '<p class="hint">Price history is unavailable right now.</p>';
  }
}

async function placePaperOrder(ticker, analysis, runId) {
  const statusEl = $(`paper-status-${ticker}`);
  const placeBtn = $(`paper-place-${ticker}`);
  if (!statusEl || !placeBtn) return; // the card was re-rendered mid-submit
  const qty = Number($(`paper-qty-${ticker}`)?.value || 0);
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
        qty,
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
    const shares = body.qty != null ? `${fmtQty(body.qty)} sh` : "";
    statusEl.innerHTML =
      `Order ${escapeHtml(body.status)}${shares ? `: ${shares}` : ""}${filled}. ` +
      '<a href="/portfolio">Open your portfolio</a>';
    placeBtn.hidden = true;
  } catch (error) {
    statusEl.textContent = "Submission failed; check the trading page for its final state.";
    placeBtn.disabled = false;
  }
}
