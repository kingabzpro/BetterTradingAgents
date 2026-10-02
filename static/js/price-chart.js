/* Compact six-month price-context chart for an expanded result (ROADMAP P1.5).
   Native SVG only, no charting dependency. Every mark is also stated in the
   text summary below the graphic, so color and pointer input are never needed. */

import { escapeHtml } from "./util.js?v=11";

const W = 640;
const H = 170;
const PAD = 16;

export async function loadPriceChart(targetId, analysis) {
  const target = document.getElementById(targetId);
  if (!target || target.dataset.loaded) return;
  target.dataset.loaded = "1";
  try {
    const response = await fetch(`/api/price-history/${encodeURIComponent(analysis.ticker)}`);
    if (!response.ok) throw new Error(`failed (${response.status})`);
    target.innerHTML = chartHtml(analysis, await response.json());
  } catch (error) {
    target.innerHTML = `<p class="hint">Price history is unavailable right now (${escapeHtml(error.message)}).</p>`;
  }
}

function chartHtml(analysis, data) {
  const dates = data.dates || [];
  const closes = data.closes || [];
  if (dates.length < 2 || closes.length < 2) {
    return `<p class="hint">No price history is available for this ticker right now.</p>`;
  }

  const asOfDate = (analysis.as_of || "").slice(0, 10);
  const analysisPrice = analysis.price;
  const forecastPrice = analysis.forecast_price_5d;
  const currentPrice = closes[closes.length - 1];

  // Mark the analysis at the last session on or before its date.
  let markIdx = -1;
  for (let i = 0; i < dates.length && dates[i] <= asOfDate; i += 1) markIdx = i;

  const yValues = closes.slice();
  if (analysisPrice != null) yValues.push(analysisPrice);
  if (forecastPrice != null) yValues.push(forecastPrice);
  const min = Math.min(...yValues);
  const max = Math.max(...yValues);
  const span = max - min || 1;
  const x = (i) => PAD + (i * (W - 2 * PAD)) / (closes.length - 1);
  const y = (v) => H - PAD - ((v - min) / span) * (H - 2 * PAD);

  const points = closes.map((c, i) => `${x(i).toFixed(1)},${y(c).toFixed(1)}`).join(" ");
  const marks = [];
  if (markIdx >= 0 && analysisPrice != null) {
    const mx = x(markIdx);
    const my = y(analysisPrice);
    // Keep the label inside the viewBox near either edge.
    const anchor = mx < W * 0.25 ? "start" : mx > W * 0.75 ? "end" : "middle";
    marks.push(
      `<circle class="chart-analysis" cx="${mx.toFixed(1)}" cy="${my.toFixed(1)}" r="4"></circle>`,
      `<text class="chart-label" x="${mx.toFixed(1)}" y="${(my - 9).toFixed(1)}" text-anchor="${anchor}">Analysis $${Number(analysisPrice).toFixed(2)} (${escapeHtml(asOfDate)})</text>`
    );
  }
  const lastX = x(closes.length - 1);
  const lastY = y(currentPrice);
  marks.push(
    `<line class="chart-current" x1="${PAD}" y1="${lastY.toFixed(1)}" x2="${(W - PAD).toFixed(1)}" y2="${lastY.toFixed(1)}"></line>`,
    `<circle class="chart-current-dot" cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5"></circle>`,
    `<text class="chart-label" x="${(W - PAD).toFixed(1)}" y="${(lastY - 8).toFixed(1)}" text-anchor="end">Current $${Number(currentPrice).toFixed(2)}</text>`
  );
  if (forecastPrice != null) {
    const fx = W - PAD;
    const fy = y(forecastPrice);
    marks.push(
      `<line class="chart-forecast" x1="${lastX.toFixed(1)}" y1="${lastY.toFixed(1)}" x2="${fx}" y2="${fy.toFixed(1)}"></line>`,
      `<circle class="chart-forecast-dot" cx="${fx}" cy="${fy.toFixed(1)}" r="3.5"></circle>`,
      `<text class="chart-label" x="${fx}" y="${(fy + 14).toFixed(1)}" text-anchor="end">Forecast $${Number(forecastPrice).toFixed(2)} (estimate)</text>`
    );
  }

  let hiIdx = 0;
  let loIdx = 0;
  closes.forEach((c, i) => {
    if (c > closes[hiIdx]) hiIdx = i;
    if (c < closes[loIdx]) loIdx = i;
  });
  const changePct = closes[0] ? ((currentPrice / closes[0]) - 1) * 100 : 0;
  const summary =
    `Six months to ${dates[dates.length - 1]}: high $${closes[hiIdx].toFixed(2)} on ${dates[hiIdx]}, `
    + `low $${closes[loIdx].toFixed(2)} on ${dates[loIdx]}, change ${changePct >= 0 ? "+" : ""}${changePct.toFixed(1)}%. `
    + (analysisPrice != null
      ? `Analysis price $${Number(analysisPrice).toFixed(2)} on ${asOfDate || "an unrecorded date"}${markIdx >= 0 ? "" : " (before this chart window)"}. `
      : "")
    + `Current $${Number(currentPrice).toFixed(2)}. `
    + (forecastPrice != null
      ? `5-day forecast $${Number(forecastPrice).toFixed(2)} (estimate).`
      : "No forecast was recorded.");

  return `
    <svg class="price-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHtml(summary)}" xmlns="http://www.w3.org/2000/svg">
      <polyline class="chart-line" points="${points}" fill="none"></polyline>
      ${marks.join("")}
    </svg>
    <p class="chart-summary">${escapeHtml(summary)}</p>`;
}
