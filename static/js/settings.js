/* Settings page: renders the field spec from GET /api/settings as a sidebar
   section layout (grouped nav with live status dots) plus a sticky save bar.
   Saves go through POST /api/settings (live apply, no restart); Reset
   restores the .env defaults. Secret values are write-only: the server
   never returns them, so blank inputs mean "unchanged" and each secret has
   an explicit "clear" checkbox. */

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value).replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

let spec = null;
let autopilot = null;
/* name -> the value the page loaded with, to detect real changes. */
let originals = {};
let activeTab = localStorage.getItem("bta_settings_tab") || "";

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 4200);
}

const SOURCE_TEXT = {
  keychain: "OS keychain",
  db: "local DB, unencrypted",
  env: "from .env",
  "not set": "not set",
};

const ROLE_CARDS = {
  manager: "Manager",
  analysts: "Analysts",
  debate: "Debate",
};

const ICONS = {
  llm: '<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
  layers: '<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
  globe: '<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
  search: '<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  shield: '<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  clock: '<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  dollar: '<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/></svg>',
  play: '<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8"/></svg>',
  flask: '<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 2h6M10 2v6L4.7 18.4A2 2 0 0 0 6.5 21h11a2 2 0 0 0 1.8-2.6L14 8V2"/><line x1="7.5" y1="14" x2="16.5" y2="14"/></svg>',
};

/* A field's live value from the current spec payload (false before load). */
function fieldOn(name) {
  if (!spec) return false;
  for (const group of spec.groups) {
    for (const field of group.fields) if (field.name === name) return !!field.value;
  }
  return false;
}

/* The Experimentation tab exists only while its master toggle is on. */
function groupVisible(group) {
  return group.key !== "experimental" || fieldOn("experimental_features");
}

/* Sidebar grouping; keys match FIELD_GROUPS keys from the API. */
const NAV = [
  {
    caption: "Connections",
    items: [
      { key: "llm", label: "LLM" },
      { key: "llm_roles", label: "Role models" },
      { key: "providers", label: "Providers" },
    ],
  },
  {
    caption: "Research",
    items: [
      { key: "analysis", label: "Analysis" },
      { key: "risk", label: "Risk" },
      { key: "memory", label: "Memory" },
    ],
  },
  {
    caption: "Paper trading",
    items: [
      { key: "alpaca", label: "Alpaca" },
      { key: "autopilot", label: "Autopilot" },
    ],
  },
  {
    caption: "Experimentation",
    items: [
      { key: "experimental", label: "Experimentation" },
    ],
  },
];

const BLURBS = {
  llm: "The OpenAI-compatible endpoint every agent calls.",
  llm_roles: "Put a cheap fast model on the researchers and a stronger one where judgment matters.",
  providers: "Optional market-data sources; each falls back gracefully without a key.",
  analysis: "How much research one analysis runs.",
  risk: "Guard rails applied to every BUY, as shares of equity.",
  memory: "How past decisions are graded and reused.",
  alpaca: "Your Alpaca paper account; paper-only, no live trading.",
  autopilot: "Scheduled research-and-trade sessions on your paper account.",
  experimental: "Retired UIs behind an opt-in; off for everyone by default.",
};

function allFields(payload) {
  const map = {};
  for (const group of payload.groups) {
    for (const field of group.fields) map[field.name] = field;
  }
  return map;
}

function inputId(name) {
  return `f-${name}`;
}

/* ---- controls ---------------------------------------------------------------- */

function fieldInput(field) {
  const id = inputId(field.name);
  if (field.type === "bool") {
    return `<input type="checkbox" id="${id}" data-name="${esc(field.name)}"${field.value ? " checked" : ""} aria-label="${esc(field.label)}">`;
  }
  if (field.choices.length) {
    const options = field.choices
      .map((choice) => `<option value="${esc(choice)}"${choice === field.value ? " selected" : ""}>${esc(choice)}</option>`)
      .join("");
    return `<select id="${id}" data-name="${esc(field.name)}">${options}</select>`;
  }
  if (field.secret) {
    return `<input type="password" id="${id}" data-name="${esc(field.name)}" autocomplete="off" ` +
      `placeholder="${field.configured ? "unchanged" : "not set"}" aria-label="${esc(field.label)}">` +
      `<button type="button" class="reveal-btn" data-reveal="${esc(field.name)}" aria-label="Show or hide ${esc(field.label)}">Show</button>`;
  }
  const type = field.type === "int" || field.type === "float" ? "number" : "text";
  const changedClass = String(field.value) !== String(field.default) ? ` class="changed"` : "";
  const step = field.type === "float" ? "any" : "1";
  const list = field.name === "llm_reasoning_effort" ? ` list="dl-reasoning"` : "";
  const placeholder = field.example ? ` placeholder="${esc(field.example)}"` : "";
  return `<input type="${type}" step="${step}"${list}${placeholder} id="${id}" data-name="${esc(field.name)}"${changedClass} ` +
    `value="${esc(field.value == null ? "" : field.value)}" aria-label="${esc(field.label)}">`;
}

function fieldRow(field) {
  originals[field.name] = field.secret ? "" : field.value;
  let extra = "";
  if (field.secret) {
    const chipClass = field.source === "db" ? "db" : field.source === "keychain" ? "keychain" : "env";
    extra =
      `<span class="source-chip ${chipClass}" data-source="${esc(field.name)}">${SOURCE_TEXT[field.source] || field.source}</span>` +
      (field.configured
        ? `<label class="clear-secret"><input type="checkbox" data-clear="${esc(field.name)}">clear</label>`
        : "");
  } else if (String(originals[field.name]) !== String(field.default)) {
    extra = `<span class="source-chip env">changed</span>`;
  }
  const hint = field.hint ? `<span class="field-hint">${esc(field.hint)}</span>` : "";
  /* The env var moves into the tooltip; the visible line shows a concrete
     example value so users know what the field expects. */
  const codeLine = field.example
    ? `<code title="Env var: ${esc(field.env)}">e.g. ${esc(field.example)}</code>`
    : `<code title="Env var: ${esc(field.env)}">${esc(field.env)}</code>`;
  return `
    <div class="field-row">
      <div class="field-label">
        <label for="${inputId(field.name)}">${esc(field.label)}</label>
        ${hint}
        ${codeLine}
      </div>
      <div class="field-input">
        ${fieldInput(field)}
        ${extra}
      </div>
    </div>`;
}

function sectionedFields(fields) {
  let html = "";
  let lastSection = "";
  for (const field of fields) {
    if (field.section && field.section !== lastSection) {
      html += `<h3 class="section-heading">${esc(field.section)}</h3>`;
    }
    lastSection = field.section;
    html += fieldRow(field);
  }
  return html;
}

/* The per-role overrides render as three side-by-side cards. */
function roleCards(fields) {
  const cards = Object.keys(ROLE_CARDS).map((role) => {
    const roleFields = fields.filter((field) => field.name.endsWith(`_${role}`));
    return `
      <div class="role-card">
        <h3>${esc(ROLE_CARDS[role])}</h3>
        ${roleFields.map(fieldRow).join("")}
      </div>`;
  });
  return `<div class="role-grid">${cards.join("")}</div>`;
}

function autopilotRow() {
  if (!autopilot) return "";
  return `
    <div class="field-row">
      <div class="field-label">
        <label for="f-autopilot-enabled">Autopilot enabled</label>
        <span class="field-hint">Same switch as the portfolio page; it lives in the database, so Reset leaves it alone.</span>
        <code>automation_state (DB)</code>
      </div>
      <div class="field-input">
        <input type="checkbox" id="f-autopilot-enabled"${autopilot.enabled ? " checked" : ""} aria-label="Autopilot enabled">
        <span class="bool-label">Sessions also need the Alpaca account and guard rails below.</span>
      </div>
    </div>`;
}

/* ---- sidebar nav ---------------------------------------------------------------- */

function renderNav() {
  $("settings-nav").innerHTML = NAV.map((section) => {
    const items = section.items.filter((item) => {
      const group = spec && spec.groups.find((candidate) => candidate.key === item.key);
      return !group || groupVisible(group);
    });
    if (!items.length) return "";
    return `<span class="nav-caption">${esc(section.caption)}</span>` +
      items.map((item) =>
        `<button type="button" class="nav-item" id="tab-${esc(item.key)}" data-tab="${esc(item.key)}" ` +
        `aria-controls="panel-${esc(item.key)}" title="${esc(BLURBS[item.key] || "")}">` +
        `${ICONS[iconFor(item.key)]}` +
        `<span>${esc(item.label)}</span>` +
        `<span class="nav-dot" data-dot="${esc(item.key)}" hidden></span>` +
        `</button>`
      ).join("");
  }).join("");
}

function iconFor(key) {
  return { llm: "llm", llm_roles: "layers", providers: "globe", analysis: "search", risk: "shield", memory: "clock", alpaca: "dollar", autopilot: "play", experimental: "flask" }[key];
}

function renderPanels() {
  $("panels").innerHTML = spec.groups
    .filter(groupVisible)
    .map((group) => {
      const body = group.key === "llm_roles"
        ? roleCards(group.fields)
        : `${group.key === "autopilot" ? autopilotRow() : ""}${sectionedFields(group.fields)}`;
      const blurb = BLURBS[group.key] ? `<span class="muted">${esc(BLURBS[group.key])}</span>` : "";
      return `<section class="card panel hidden" id="panel-${esc(group.key)}" aria-labelledby="tab-${esc(group.key)}">
        <div class="group-heading"><h2>${esc(group.label)}</h2>${blurb}</div>
        <div class="fields">${body}</div>
      </section>`;
    })
    .join("");
}

function switchTab(key) {
  const tabIds = spec.groups.filter(groupVisible).map((group) => group.key);
  if (!tabIds.includes(key)) key = tabIds[0];
  activeTab = key;
  localStorage.setItem("bta_settings_tab", key);
  for (const id of tabIds) {
    const selected = id === key;
    $(`tab-${id}`).setAttribute("aria-current", String(selected));
    $(`panel-${id}`).classList.toggle("hidden", !selected);
  }
}

/* ---- sidebar dots: one ready-state dot per section ------------------------------- */

function setDot(key, state, title) {
  const dot = document.querySelector(`[data-dot="${key}"]`);
  if (!dot) return;
  dot.hidden = !state;
  dot.className = `nav-dot ${state || ""}`;
  dot.title = title || "";
}

function updateNavDots() {
  const fields = allFields(spec);
  const llmOn = fields.llm_api_key.configured;
  setDot("llm", llmOn ? "ok" : "bad",
    llmOn ? `Agents live on ${fields.llm_model.value}` : "No LLM key: the app runs in mock mode");

  const roleNames = ["llm_model_manager", "llm_model_analysts", "llm_model_debate",
    "llm_base_url_manager", "llm_base_url_analysts", "llm_base_url_debate",
    "llm_api_key_manager", "llm_api_key_analysts", "llm_api_key_debate"];
  const roleCount = roleNames.filter((name) => fields[name].secret
    ? fields[name].configured
    : String(fields[name].value || "").trim() !== "").length;
  setDot("llm_roles", roleCount ? "ok" : "off",
    roleCount ? `${roleCount} per-role override${roleCount === 1 ? "" : "s"} set` : "Every role uses the global LLM");

  const missing = [["finnhub_api_key", "Finnhub"], ["olostep_api_key", "Olostep"], ["nixtla_api_key", "TimeGPT"]]
    .filter(([name]) => !fields[name].configured)
    .map(([, label]) => label);
  if (missing.length === 0) setDot("providers", "ok", "All three providers connected");
  else if (missing.length === 3) setDot("providers", "off", "No optional providers; built-in fallbacks in use");
  else setDot("providers", "warn", `Not set: ${missing.join(", ")}`);

  const alpacaOn = fields.alpaca_api_key_id.configured && fields.alpaca_api_secret_key.configured;
  if (!alpacaOn) setDot("alpaca", "bad", "Not connected: paper trading stays dormant");
  else if (!fields.alpaca_trading_enabled.value) setDot("alpaca", "warn", "Connected, but read-only (kill switch off)");
  else setDot("alpaca", "ok", "Paper account connected, submissions allowed");

  setDot("autopilot", autopilot && autopilot.enabled ? "ok" : "off",
    autopilot && autopilot.enabled ? "Autopilot sessions are on" : "Autopilot sessions are off");
}

/* ---- dirty tracking ---------------------------------------------------------------- */

function collectChanges() {
  const values = {};
  const clears = [];
  const names = [];
  for (const group of spec.groups) {
    for (const field of group.fields) {
      const input = $(inputId(field.name));
      if (!input) continue;
      if (field.secret) {
        if (input.value.trim() !== "") {
          values[field.name] = input.value.trim();
          names.push(field.name);
        }
        continue;
      }
      const current = field.type === "bool" ? input.checked : input.value.trim();
      if (String(current) !== String(originals[field.name])) {
        values[field.name] = current;
        names.push(field.name);
      }
      input.classList.toggle("changed", String(current) !== String(field.default));
    }
  }
  document.querySelectorAll("[data-clear]").forEach((box) => {
    if (box.checked) {
      clears.push(box.dataset.clear);
      names.push(box.dataset.clear);
    }
  });
  return { values, clears, names, changed: names.length > 0 };
}

function updateDirty() {
  const { names } = collectChanges();
  $("dirty-note").textContent = names.length
    ? `${names.length} unsaved change${names.length === 1 ? "" : "s"}`
    : "No changes";
  $("save-btn").disabled = names.length === 0;
}

/* ---- save, reset, autopilot ---------------------------------------------------------- */

async function setAutopilot(enabled) {
  try {
    const response = await fetch("/api/automation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!response.ok) throw new Error(`failed (${response.status})`);
    autopilot = await response.json();
    $("f-autopilot-enabled").checked = autopilot.enabled;
    updateNavDots();
    showToast(autopilot.enabled ? "Autopilot enabled" : "Autopilot disabled");
  } catch (error) {
    $("f-autopilot-enabled").checked = !enabled;
    showToast(`Could not update autopilot: ${error.message}`, true);
  }
}

async function save() {
  const { values, clears, changed } = collectChanges();
  if (!changed) return;
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values, clear_secrets: clears }),
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error(body?.detail || `failed (${response.status})`);
    const cleared = clears.length ? `; cleared ${clears.length}` : "";
    showToast(`Settings saved and applied${cleared}`);
    render(body);
  } catch (error) {
    showToast(`Could not save settings: ${error.message}`, true);
  }
}

async function resetToDefaults() {
  if (!window.confirm("Reset every setting to its .env default? Saved keys are removed from the OS keychain.")) return;
  try {
    const response = await fetch("/api/settings/reset", { method: "POST" });
    if (!response.ok) throw new Error(`failed (${response.status})`);
    showToast("Settings reset to .env defaults");
    render(await response.json());
  } catch (error) {
    showToast(`Could not reset settings: ${error.message}`, true);
  }
}

/* ---- render and wiring ------------------------------------------------------------------ */

function render(payload) {
  spec = payload;
  originals = {};
  $("insecure-note").classList.toggle("hidden", payload.keychain_available);
  renderNav();
  renderPanels();
  switchTab(activeTab);
  updateNavDots();
  $("save-btn").disabled = false;
  $("reset-btn").disabled = false;
  updateDirty();
}

/* One delegated listener set on the persistent containers covers every re-render. */
function wireEvents() {
  $("panels").addEventListener("input", updateDirty);
  $("panels").addEventListener("change", (event) => {
    if (event.target.id === "f-autopilot-enabled") setAutopilot(event.target.checked);
  });
  $("panels").addEventListener("click", (event) => {
    const reveal = event.target.closest("[data-reveal]");
    if (!reveal) return;
    const input = $(inputId(reveal.dataset.reveal));
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    reveal.textContent = show ? "Hide" : "Show";
  });
  $("settings-nav").addEventListener("click", (event) => {
    const item = event.target.closest("[data-tab]");
    if (item && item.dataset.tab) switchTab(item.dataset.tab);
  });
  $("settings-nav").addEventListener("keydown", (event) => {
    if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
    const items = [...document.querySelectorAll("#settings-nav .nav-item")];
    const index = items.findIndex((item) => item.getAttribute("aria-current") === "true");
    const next = event.key === "ArrowDown" ? index + 1 : index - 1;
    const target = items[(next + items.length) % items.length];
    switchTab(target.dataset.tab);
    target.focus();
    event.preventDefault();
  });
  $("save-btn").addEventListener("click", save);
  $("reset-btn").addEventListener("click", resetToDefaults);
}

async function load() {
  try {
    const [payload, automationStatus] = await Promise.all([
      fetch("/api/settings").then((r) => (r.ok ? r.json() : Promise.reject(new Error(`failed (${r.status})`)))),
      fetch("/api/automation").then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    autopilot = automationStatus;
    render(payload);
  } catch (error) {
    $("dirty-note").textContent = `Could not load settings: ${error.message}`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  wireEvents();
  load();
});
