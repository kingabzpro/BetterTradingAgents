/* Experimental feature gates, shared by every page. One fetch decides which
   retired UIs are live; window.BTA_FEATURES feeds the result-card/History
   action gating in render.js and history.js, and the header nav is normalized
   in place: enabled features get their link (before Trades), disabled ones
   lose any link the static HTML still carries. */

(async () => {
  window.BTA_FEATURES = window.BTA_FEATURES || {};
  try {
    const response = await fetch("/api/features");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    Object.assign(window.BTA_FEATURES, await response.json());
  } catch (_) {
    /* features stay off when the endpoint is unreachable */
  }
  const items = [
    { key: "accuracy", label: "Accuracy", href: "/accuracy" },
    { key: "compare", label: "Compare", href: "/compare" },
    { key: "watchlist", label: "Watchlist", href: "/watchlist" },
  ];
  const nav = document.querySelector('nav[aria-label="Primary navigation"]');
  if (!nav) return;
  const anchor = nav.querySelector("a[href='/portfolio']"); // insert before Trades
  for (const item of items) {
    const existing = nav.querySelector(`a[href='${item.href}']`);
    if (window.BTA_FEATURES[item.key]) {
      if (!existing && anchor) {
        const link = document.createElement("a");
        link.href = item.href;
        link.className = "nav-link";
        link.textContent = item.label;
        nav.insertBefore(link, anchor);
      }
    } else if (existing) {
      existing.remove();
    }
  }
})();
