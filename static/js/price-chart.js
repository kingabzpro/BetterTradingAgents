/* Price-context chart v2: axes, gridlines, hover crosshair, and a shaded
   forecast band. Native SVG, no charting dependency. Every mark is also
   stated in the text summary below the graphic, so color and pointer input
   are never required. */

import { escapeHtml } from "./util.js";

const HEIGHT = 190;
const PAD = { top: 14, right: 16, bottom: 24, left: 46 };
const FUTURE_STEPS = 6; // legacy fallback when an old run has no forecast path

const state = new WeakMap();
let resizeTimer = null;

export async function loadPriceChart(targetId, analysis, prefetched = null) {
  const target = document.getElementById(targetId);
  if (!target || target.dataset.loaded) return;
  target.dataset.loaded = "1";
  try {
    const data = prefetched || await fetch(`/api/price-history/${encodeURIComponent(analysis.ticker)}`)
      .then((response) => {
        if (!response.ok) throw new Error(`failed (${response.status})`);
        return response.json();
      });
    state.set(target, { analysis, data });
    renderChart(target);
    // One shared resize listener redraws at the new width (crisp text,
    // exact hover math; SVG scaling would blur both).
    if (!loadPriceChart.wired) {
      loadPriceChart.wired = true;
      window.addEventListener("resize", () => {
        window.clearTimeout(resizeTimer);
        resizeTimer = window.setTimeout(() => {
          document.querySelectorAll(".chart-holder").forEach((el) => {
            if (state.has(el)) renderChart(el);
          });
        }, 150);
      });
    }
  } catch (error) {
    target.innerHTML = `<p class="hint">Price history is unavailable right now (${escapeHtml(error.message)}).</p>`;
  }
}

function renderChart(target) {
  const { analysis, data } = state.get(target);
  const dates = data.dates || [];
  const closes = data.closes || [];
  if (dates.length < 2 || closes.length < 2) {
    target.innerHTML = `<p class="hint">No price history is available for this ticker right now.</p>`;
    return;
  }

  const asOfDate = (analysis.as_of || "").slice(0, 10);
  const analysisPrice = analysis.price;
  const forecastPrice = analysis.forecast_price_5d;
  const bandPct = analysis.forecast_band_pct;
  const currentPrice = closes[closes.length - 1];
  // Per-step forecast prices out to the horizon (Bako path or local ridge
  // path). One point per trading step, so the future part of the chart is
  // drawn at its real length instead of a fixed 6-slot stub.
  const forecastPath = (Array.isArray(analysis.forecast_path) ? analysis.forecast_path : [])
    .filter((v) => v != null && Number.isFinite(v))
    .map(Number);
  const hasPath = forecastPrice != null && forecastPath.length >= 1;

  let markIdx = -1;
  for (let i = 0; i < dates.length && dates[i] <= asOfDate; i += 1) markIdx = i;

  // Domain: the price series plus every mark that must stay inside the frame.
  const yValues = closes.slice();
  if (analysisPrice != null) yValues.push(analysisPrice);
  if (forecastPrice != null) {
    yValues.push(forecastPrice);
    if (hasPath) yValues.push(...forecastPath);
    if (bandPct != null) {
      yValues.push(forecastPrice * (1 + bandPct / 100));
      yValues.push(forecastPrice * (1 - bandPct / 100));
    }
  }
  let min = Math.min(...yValues);
  let max = Math.max(...yValues);
  const pad = (max - min || 1) * 0.06;
  min -= pad;
  max += pad;

  const W = Math.max(320, Math.round(target.clientWidth || 640));
  const innerW = W - PAD.left - PAD.right;
  const innerH = HEIGHT - PAD.top - PAD.bottom;
  const future = forecastPrice != null ? (hasPath ? forecastPath.length : FUTURE_STEPS) : 0;
  const total = closes.length - 1 + future;
  const x = (i) => PAD.left + (i * innerW) / total;
  const y = (v) => PAD.top + innerH - ((v - min) / (max - min)) * innerH;

  // Gridlines with value labels (4 rows, honest scale, no smoothing).
  const grid = [];
  for (let t = 0; t <= 3; t += 1) {
    const value = max - ((max - min) * t) / 3;
    const gy = y(value);
    grid.push(
      `<line class="chart-grid" x1="${PAD.left}" y1="${gy.toFixed(1)}" x2="${(W - PAD.right).toFixed(1)}" y2="${gy.toFixed(1)}"></line>`,
      `<text class="chart-label" x="${PAD.left - 7}" y="${(gy + 3).toFixed(1)}" text-anchor="end">$${value.toFixed(2)}</text>`
    );
  }

  // Date labels: first, middle, last.
  const midIdx = Math.floor((closes.length - 1) / 2);
  const dateLabels = [
    `<text class="chart-label" x="${x(0).toFixed(1)}" y="${HEIGHT - 6}" text-anchor="start">${escapeHtml(dates[0])}</text>`,
    `<text class="chart-label" x="${x(midIdx).toFixed(1)}" y="${HEIGHT - 6}" text-anchor="middle">${escapeHtml(dates[midIdx])}</text>`,
    `<text class="chart-label" x="${(W - PAD.right).toFixed(1)}" y="${HEIGHT - 6}" text-anchor="end">${escapeHtml(dates[dates.length - 1])}</text>`,
  ];

  const points = closes.map((c, i) => `${x(i).toFixed(1)},${y(c).toFixed(1)}`).join(" ");
  const base = (PAD.top + innerH).toFixed(1);
  const area = `${x(0).toFixed(1)},${base} ${points} ${(x(closes.length - 1)).toFixed(1)},${base}`;

  const marks = [];
  if (markIdx >= 0 && analysisPrice != null) {
    const mx = x(markIdx);
    const my = y(analysisPrice);
    const anchor = mx < W * 0.25 ? "start" : mx > W * 0.75 ? "end" : "middle";
    // A run dated near the latest session prints its marker beside the
    // Current label; lift it clear when the two would overprint.
    const currentY = y(closes[closes.length - 1]);
    const nearCurrent = mx > W - PAD.right - 170 && Math.abs(my - currentY) < 26;
    marks.push(
      `<circle class="chart-analysis" cx="${mx.toFixed(1)}" cy="${my.toFixed(1)}" r="4"></circle>`,
      `<text class="chart-label" x="${mx.toFixed(1)}" y="${(my - (nearCurrent ? 26 : 9)).toFixed(1)}" text-anchor="${anchor}">Analysis $${Number(analysisPrice).toFixed(2)} (${escapeHtml(asOfDate)})</text>`
    );
  }
  const lastX = x(closes.length - 1);
  const lastY = y(currentPrice);
  marks.push(
    `<line class="chart-current" x1="${PAD.left}" y1="${lastY.toFixed(1)}" x2="${(W - PAD.right).toFixed(1)}" y2="${lastY.toFixed(1)}"></line>`,
    `<circle class="chart-current-dot" cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5"></circle>`,
    `<text class="chart-label" x="${(W - PAD.right - 8).toFixed(1)}" y="${(lastY - 8).toFixed(1)}" text-anchor="end">Current $${Number(currentPrice).toFixed(2)}</text>`
  );
  if (forecastPrice != null) {
    const fx = x(closes.length - 1 + future);
    const fy = y(forecastPrice);
    if (bandPct != null) {
      const upper = forecastPrice * (1 + bandPct / 100);
      const lower = forecastPrice * (1 - bandPct / 100);
      marks.push(
        `<polygon class="chart-band" points="${lastX.toFixed(1)},${lastY.toFixed(1)} ${fx.toFixed(1)},${y(upper).toFixed(1)} ${fx.toFixed(1)},${y(lower).toFixed(1)}"></polygon>`,
        `<text class="chart-label" x="${fx.toFixed(1)}" y="${(y(upper) - 6).toFixed(1)}" text-anchor="end">±${Number(bandPct).toFixed(1)}% band</text>`
      );
    }
    if (hasPath) {
      // The full per-step path: one point per trading step after the last close.
      const pathPoints = forecastPath
        .map((p, i) => `${x(closes.length + i).toFixed(1)},${y(p).toFixed(1)}`)
        .join(" ");
      marks.push(`<polyline class="chart-forecast" points="${lastX.toFixed(1)},${lastY.toFixed(1)} ${pathPoints}" fill="none"></polyline>`);
    } else {
      // Legacy rows without a path: one straight segment to the endpoint.
      marks.push(`<line class="chart-forecast" x1="${lastX.toFixed(1)}" y1="${lastY.toFixed(1)}" x2="${fx.toFixed(1)}" y2="${fy.toFixed(1)}"></line>`);
    }
    // Keep the forecast label clear of the current-price label when the two
    // points are close in price (their y positions can otherwise collide).
    const forecastLabelY = Math.abs(fy - lastY) < 24 ? Math.max(fy, lastY) + 26 : fy + 14;
    marks.push(
      `<circle class="chart-forecast-dot" cx="${fx.toFixed(1)}" cy="${fy.toFixed(1)}" r="3.5"></circle>`,
      `<text class="chart-label" x="${fx.toFixed(1)}" y="${forecastLabelY.toFixed(1)}" text-anchor="end">Forecast $${Number(forecastPrice).toFixed(2)} (estimate)</text>`
    );
  }

  let hiIdx = 0;
  let loIdx = 0;
  closes.forEach((c, i) => {
    if (c > closes[hiIdx]) hiIdx = i;
    if (c < closes[loIdx]) loIdx = i;
  });
  const changePct = closes[0] ? ((currentPrice / closes[0]) - 1) * 100 : 0;
  const horizonDays = Number(analysis.forecast_horizon_days) || 5;
  const horizonLabel = horizonDays === 1 ? "Next-session forecast" : `${horizonDays}-day forecast`;
  const summary =
    `Six months to ${dates[dates.length - 1]}: high $${closes[hiIdx].toFixed(2)} on ${dates[hiIdx]}, `
    + `low $${closes[loIdx].toFixed(2)} on ${dates[loIdx]}, change ${changePct >= 0 ? "+" : ""}${changePct.toFixed(1)}%. `
    + (analysisPrice != null
      ? `Analysis price $${Number(analysisPrice).toFixed(2)} on ${asOfDate || "an unrecorded date"}${markIdx >= 0 ? "" : " (before this chart window)"}. `
      : "")
    + `Current $${Number(currentPrice).toFixed(2)}. `
    + (forecastPrice != null
      ? `${horizonLabel} $${Number(forecastPrice).toFixed(2)} (estimate${bandPct != null ? `, ±${Number(bandPct).toFixed(1)}% noise band` : ""}).`
      : "No forecast was recorded.");

  target.innerHTML = `
    <svg class="price-chart" viewBox="0 0 ${W} ${HEIGHT}" role="img" aria-label="${escapeHtml(summary)}" xmlns="http://www.w3.org/2000/svg">
      ${grid.join("")}
      ${dateLabels.join("")}
      <polygon class="chart-area" points="${area}"></polygon>
      <polyline class="chart-line" points="${points}" fill="none"></polyline>
      ${marks.join("")}
      <g class="chart-hover" hidden aria-hidden="true">
        <line class="chart-hover-line" y1="${PAD.top}" y2="${PAD.top + innerH}"></line>
        <circle class="chart-hover-dot" r="3.5"></circle>
      </g>
    </svg>
    <div class="chart-tip" hidden></div>
    <p class="chart-summary">${escapeHtml(summary)}</p>`;

  wireHover(target, { dates, closes, x, y, W });
}

function wireHover(target, { dates, closes, x, y, W }) {
  const svg = target.querySelector("svg");
  const hover = target.querySelector(".chart-hover");
  const line = target.querySelector(".chart-hover-line");
  const dot = target.querySelector(".chart-hover-dot");
  const tip = target.querySelector(".chart-tip");
  svg.addEventListener("mousemove", (event) => {
    const px = event.offsetX;
    const i = Math.max(0, Math.min(closes.length - 1, Math.round(((px - PAD.left) / (W - PAD.left - PAD.right)) * (closes.length - 1))));
    line.setAttribute("x1", x(i).toFixed(1));
    line.setAttribute("x2", x(i).toFixed(1));
    dot.setAttribute("cx", x(i).toFixed(1));
    dot.setAttribute("cy", y(closes[i]).toFixed(1));
    tip.textContent = `${dates[i]} · $${Number(closes[i]).toFixed(2)}`;
    tip.hidden = false;
    hover.classList.add("on");
    const tipX = Math.min(Math.max(px - 60, 4), W - 150);
    tip.style.left = `${tipX}px`;
  });
  svg.addEventListener("mouseleave", () => {
    hover.classList.remove("on");
    tip.hidden = true;
  });
}
