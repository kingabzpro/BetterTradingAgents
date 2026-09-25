/* Save a finished analysis to the SQLite watchlist (ROADMAP P1.4). */

import { $, getClientId, showToast } from "./util.js";
import { CLIENT_ID_KEY } from "./constants.js";

export async function saveToWatchlist(ticker, runId, note = "") {
  const button = $(`watch-${ticker}`);
  const noteEl = $(`watched-${ticker}`);
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/watchlist", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Client-ID": getClientId(CLIENT_ID_KEY),
      },
      body: JSON.stringify({ ticker, note, run_id: runId || null }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(
        (typeof body.detail === "string" && body.detail) || `failed (${response.status})`
      );
    }
    if (noteEl) {
      noteEl.textContent = body.already_watched
        ? "✓ Already on watchlist (baseline kept)"
        : "✓ Saved to watchlist";
      noteEl.classList.remove("hidden");
    }
    showToast(
      body.already_watched
        ? `${ticker} is already on the watchlist`
        : `${ticker} saved to watchlist`
    );
    if (button) {
      button.textContent = "Watching";
      button.disabled = true;
    }
  } catch (error) {
    showToast(`Could not save ${ticker}: ${error.message}`, true);
    if (button) button.disabled = false;
  }
}
