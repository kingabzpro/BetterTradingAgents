/* Add-to-demo-portfolio action used by BUY result cards. */

import { $, showToast } from "./util.js";

export async function addToPortfolio(ticker, price) {
  const button = $(`add-${ticker}`);
  const qtyInput = $(`qty-${ticker}`);
  const note = $(`added-${ticker}`);
  const quantity = Number(qtyInput.value);
  if (!quantity || quantity <= 0) { showToast("Enter a share quantity first", true); qtyInput.focus(); return; }
  button.disabled = true;
  try {
    const response = await fetch("/api/portfolio/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticker, quantity, entry_price: price }) });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `failed (${response.status})`);
    }
    note.textContent = `✓ ${quantity} ${ticker} @ $${price.toFixed(2)} added`;
    note.classList.remove("hidden");
    showToast(`${ticker} added to demo portfolio`);
  } catch (error) {
    showToast(`Could not add position: ${error.message}`, true);
    button.disabled = false;
  }
}
