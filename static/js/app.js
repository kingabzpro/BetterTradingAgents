/* BetterTradingAgents - analysis screen entry point.
   Wires the DOM and boots recovery / rerun, then hands off to run.js. */

import { ADVANCED_OPEN_KEY, CLIENT_ID_KEY, DEPTH_KEY, OUTLOOK_KEY } from "./constants.js";
import { setDepth, setOutlook, updateAdvancedSummary, updateDepthLabels } from "./options.js";
import { state } from "./state.js";
import { addTickerTags, analyzeAnother, removeTickerTag, renderTickerTags } from "./tickers.js";
import { applyRerunParams, cancelRun, feelingLucky, restoreSavedRun, startAnalysis } from "./run.js";
import { $, getClientId } from "./util.js";

document.addEventListener("DOMContentLoaded", async () => {
  $("analyze-btn").addEventListener("click", () => startAnalysis());
  $("feeling-lucky-btn").addEventListener("click", () => feelingLucky());
  $("analyze-another-btn").addEventListener("click", () => analyzeAnother());
  $("run-again-btn").addEventListener("click", () => { if (!state.running && !state.discovering) startAnalysis(); });
  $("cancel-btn").addEventListener("click", () => cancelRun());
  $("add-ticker-btn").addEventListener("click", () => addTickerTags($("ticker-input").value));
  $("ticker-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      if ($("ticker-input").value.trim()) addTickerTags($("ticker-input").value);
      else startAnalysis();
    }
    if (event.key === "Backspace" && !$("ticker-input").value && state.tickerTags.length) {
      removeTickerTag(state.tickerTags[state.tickerTags.length - 1]);
    }
  });
  document.querySelectorAll(".chip-btn").forEach((chip) => {
    chip.addEventListener("click", () => addTickerTags(chip.dataset.tickers));
  });
  document.querySelectorAll("[data-outlook]").forEach((button) => {
    button.addEventListener("click", () => setOutlook(button.dataset.outlook));
  });
  document.querySelectorAll("[data-depth]").forEach((button) => {
    button.addEventListener("click", () => setDepth(button.dataset.depth));
  });
  $("advanced-options").addEventListener("toggle", () => {
    try { localStorage.setItem(ADVANCED_OPEN_KEY, $("advanced-options").open ? "1" : "0"); } catch (_) {}
  });
  try { if (localStorage.getItem(ADVANCED_OPEN_KEY) === "1") $("advanced-options").open = true; } catch (_) {}
  try {
    setOutlook(localStorage.getItem(OUTLOOK_KEY));
    setDepth(localStorage.getItem(DEPTH_KEY));
  } catch (_) {}
  updateAdvancedSummary();
  renderTickerTags();
  // Touch the client id once at boot so the run-history scope exists.
  getClientId(CLIENT_ID_KEY);

  try {
    const response = await fetch("/api/health");
    const health = await response.json();
    if (health.mock_mode) $("mode-chip").classList.remove("hidden");
    state.debateRounds = health.debate_rounds || 1;
    state.maxTickers = health.max_tickers || 5;
    updateDepthLabels();
    const providers = health.providers || {};
    $("provider-line").textContent =
      `data: ${[providers.prices, providers.fundamentals, providers.news_search, providers.forecast].filter((s) => s && s !== "disabled").join(" · ")}` +
      ` · model: ${health.llm_model || "mock"}`;
  } catch (_) {
    // The analysis request will show a concrete error if the server is unavailable.
  }
  if (await applyRerunParams()) return;
  await restoreSavedRun();
});
