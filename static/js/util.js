/* Shared DOM and formatting helpers. */

export const $ = (id) => document.getElementById(id);

export function fmtUsd(value) { return `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`; }

// Estimated model cost: single run totals are cents, so keep more decimals
// until whole dollars make them noise.
export function fmtCostUsd(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return "$0";
  if (n < 1) return `$${n.toFixed(4)}`;
  if (n < 1000) return `$${n.toFixed(2)}`;
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

export function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" }).format(date);
}

export function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function safeUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_) { return ""; }
}

export function getClientId(storageKey) {
  try {
    const existing = localStorage.getItem(storageKey);
    if (existing) return existing;
    const created = globalThis.crypto?.randomUUID
      ? globalThis.crypto.randomUUID()
      : `device_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(storageKey, created);
    return created;
  } catch (_) {
    return `device_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  }
}

export function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

export function escapeAttr(text) { return escapeHtml(text).replaceAll('"', "&quot;").replaceAll("'", "&#39;"); }

export function showError(message) { $("error-msg").textContent = message; $("error-msg").classList.remove("hidden"); }
export function hideError() { $("error-msg").classList.add("hidden"); }

let toastTimer = null;
export function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  toast.setAttribute("role", isError ? "alert" : "status");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 4200);
}
