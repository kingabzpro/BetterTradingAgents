/* BetterTradingAgents - Accuracy page: graded past calls vs realized
   performance (ROADMAP P1.9). One fetch, all rendering client-side. */

const $ = (id) => document.getElementById(id);

const DECISION_META = {
  BUY: { className: "buy", icon: "▲", label: "BUY" },
  HOLD: { className: "hold", icon: "■", label: "HOLD" },
  SELL: { className: "sell", icon: "▼", label: "SELL" },
};
const VERDICT_META = {
  right: { className: "right", label: "Right" },
  wrong: { className: "wrong", label: "Wrong" },
  neutral: { className: "neutral", label: "Neutral" },
  unknown: { className: "neutral", label: "Unknown" },
};

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

function money(pct) {
  if (pct == null) return '<span class="muted">n/a</span>';
  const dollars = (pct / 100) * 10000;
  return `${dollars < 0 ? "-" : "+"}$${Math.abs(Math.round(dollars)).toLocaleString("en-US")}`;
}

function badge(decision) {
  const meta = DECISION_META[decision] || DECISION_META.HOLD;
  return `<span class="decision ${meta.className}">${meta.icon} ${meta.label}</span>`;
}

function verdictPill(row) {
  const meta = VERDICT_META[row.verdict] || VERDICT_META.unknown;
  const why = row.lesson ? ` title="${escapeAttr(row.lesson)}"` : "";
  return `<span class="verdict ${meta.className}"${why}>${meta.label}</span>`;
}

function pct(value, digits = 1) {
  if (value == null) return '<span class="muted">n/a</span>';
  return `${value >= 0 ? "+" : ""}${Number(value).toFixed(digits)}%`;
}

function renderAggregates(report) {
  $("accuracy-aggregates").innerHTML = report.by_decision
    .filter((group) => group.n > 0)
    .map((group) => {
      const meta = DECISION_META[group.decision] || DECISION_META.HOLD;
      const hit = group.hit_rate == null
        ? '<span class="agg-hit muted">no decisive calls yet</span>'
        : `<span class="agg-hit">${Math.round(group.hit_rate * 100)}%</span> <small>right of decisive calls</small>`;
      const alpha = group.mean_alpha_pct == null
        ? ""
        : `<div class="agg-detail">mean alpha ${pct(group.mean_alpha_pct)}</div>`;
      const realized = group.mean_realized_pct == null
        ? ""
        : `<div class="agg-detail">mean realized ${pct(group.mean_realized_pct)}</div>`;
      // HOLD's right/wrong is about what standing aside did, so label it that way.
      const detail = group.decision === "HOLD"
        ? `n=${group.n} · ${group.right} slides avoided · ${group.wrong} gains missed · ${group.neutral} neutral`
        : `n=${group.n} · ${group.right} right · ${group.wrong} wrong · ${group.neutral} neutral`;
      return `<article class="agg-card">
        <span class="decision ${meta.className}">${meta.icon} ${meta.label} calls</span>
        <div class="agg-main">${hit}</div>
        <div class="agg-detail">${detail}</div>
        ${alpha}
        ${realized}
      </article>`;
    }).join("");
  $("accuracy-aggregates").classList.toggle("hidden", report.graded === 0);
}

function renderRows(report) {
  const tbody = $("accuracy-table").querySelector("tbody");
  tbody.innerHTML = report.rows.map((row) => `<tr>
    <td><strong>${escapeHtml(row.ticker)}</strong></td>
    <td>${escapeHtml(row.date)}</td>
    <td>${badge(row.decision)}</td>
    <td class="num">${Math.round((row.confidence || 0) * 100)}%</td>
    <td class="num">$${Number(row.entry_price || 0).toFixed(2)}</td>
    <td class="num">${pct(row.realized_return_pct)}</td>
    <td class="num">${pct(row.spy_return_pct)}</td>
    <td class="num">${pct(row.alpha_vs_spy_pct)}</td>
    <td>${verdictPill(row)}</td>
  </tr>`).join("");
  $("accuracy-table-card").classList.toggle("hidden", report.rows.length === 0);
}

function render(report) {
  document.querySelectorAll(".horizon-fill").forEach((node) => {
    node.textContent = `${report.horizon_days}-day`;
  });
  renderAggregates(report);
  renderRows(report);
  $("accuracy-empty").classList.toggle("hidden", report.graded > 0);

  const small = $("accuracy-small-sample");
  if (report.graded > 0 && report.graded < 10) {
    small.textContent = `Small sample: ${report.graded} graded call${report.graded === 1 ? "" : "s"}. Read these percentages as curiosities, not statistics.`;
    small.classList.remove("hidden");
  } else {
    small.classList.add("hidden");
  }

  const perSignal = report.per_signal || {};
  const line = $("accuracy-per-signal");
  if (perSignal.n > 0 && perSignal.always_buy_pct != null) {
    line.innerHTML = `If every call had been followed with $10,000 per signal: <strong>following the calls ${money(perSignal.follow_calls_pct)}</strong> · always-buy ${money(perSignal.always_buy_pct)} · SPY ${money(perSignal.spy_pct)} <small>(mean over ${perSignal.n} graded window${perSignal.n === 1 ? "" : "s"}, long-only)</small>.`;
    line.classList.remove("hidden");
  } else {
    line.classList.add("hidden");
  }

  const slides = $("accuracy-slides");
  if (report.avoided_slides && report.avoided_slides.length > 0) {
    const list = report.avoided_slides
      .map((slide) => `${escapeHtml(slide.ticker)} ${Number(slide.realized_pct).toFixed(0)}%`)
      .join(" · ");
    slides.innerHTML = `Standing aside sidestepped ${report.avoided_slides.length} double-digit slide${report.avoided_slides.length === 1 ? "" : "s"}: ${list}.`;
    slides.classList.remove("hidden");
  } else {
    slides.classList.add("hidden");
  }

  const pending = $("accuracy-pending");
  if (report.pending > 0) {
    const plural = report.pending === 1 ? "call is" : "calls are";
    pending.textContent = `${report.pending} ${plural} still inside the ${report.horizon_days}-day window and not graded yet.`;
    pending.classList.remove("hidden");
  } else {
    pending.classList.add("hidden");
  }
  $("accuracy-status").classList.add("hidden");
}

async function load() {
  try {
    const response = await fetch("/api/accuracy");
    if (!response.ok) throw new Error(`the server returned ${response.status}`);
    render(await response.json());
  } catch (error) {
    $("accuracy-status").textContent = `Accuracy report unavailable: ${error.message}. Try refreshing.`;
  }
}

document.addEventListener("DOMContentLoaded", load);
