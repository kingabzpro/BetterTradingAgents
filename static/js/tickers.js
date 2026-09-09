/* Ticker tag input plus the post-run retry / analyze-another actions. */

import { TICKER_PATTERN } from "./constants.js";
import { state } from "./state.js";
import { $, escapeAttr, escapeHtml, hideError, showToast, showError } from "./util.js";

export function addTickerTags(raw) {
  const parts = String(raw || "")
    .split(/[\s,]+/)
    .map((ticker) => ticker.trim().toUpperCase())
    .filter(Boolean);
  if (!parts.length) return;
  const invalid = parts.filter((ticker) => !TICKER_PATTERN.test(ticker));
  if (invalid.length) {
    showError(`That does not look like a ticker symbol: ${invalid.join(", ")}.`);
    return;
  }
  const next = [...state.tickerTags];
  for (const ticker of parts) {
    if (!next.includes(ticker)) next.push(ticker);
  }
  if (next.length > state.maxTickers) {
    showError(`Max ${state.maxTickers} tickers at once. Remove one before adding more.`);
    return;
  }
  hideError();
  state.tickerTags = next;
  $("ticker-input").value = "";
  renderTickerTags();
}

export function removeTickerTag(ticker) {
  state.tickerTags = state.tickerTags.filter((existing) => existing !== ticker);
  renderTickerTags();
  // The remove button is gone from the DOM, so focus fell back to the page;
  // keep keyboard users on the control that owns the tag list.
  if (!document.activeElement || document.activeElement === document.body) $("ticker-input").focus();
}

export function setTickerTags(raw) {
  state.tickerTags = String(raw || "")
    .split(/[\s,]+/)
    .map((ticker) => ticker.trim().toUpperCase())
    .filter((ticker) => TICKER_PATTERN.test(ticker))
    .slice(0, state.maxTickers);
  renderTickerTags();
}

export function renderTickerTags() {
  $("ticker-tags").innerHTML = state.tickerTags.map((ticker) => `
    <span class="ticker-tag">${escapeHtml(ticker)}<button type="button" class="tag-remove" data-remove="${escapeAttr(ticker)}" aria-label="Remove ${escapeAttr(ticker)}">×</button></span>`
  ).join("");
  $("ticker-tags").querySelectorAll("[data-remove]").forEach((button) => {
    button.addEventListener("click", () => removeTickerTag(button.dataset.remove));
  });
  $("add-ticker-btn").disabled = state.tickerTags.length >= state.maxTickers;
  if (!state.running && !state.discovering) $("analyze-btn").disabled = state.tickerTags.length === 0;
}

export function analyzeAnother(prefill = "") {
  if (state.running) return;
  if (prefill) setTickerTags(prefill);
  $("ticker-input").focus();
  $("ticker-input").scrollIntoView({ behavior: "smooth", block: "center" });
}

export function retryTicker(ticker) {
  analyzeAnother(ticker);
  showToast(`${ticker} ready to retry.`);
}
