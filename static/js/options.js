/* Outlook and depth selectors plus the depth-profile helpers. */

import {
  AGENTS, DEPTH_KEY, DEPTH_PROFILES, OUTLOOKS, OUTLOOK_KEY, OUTLOOK_LABELS,
} from "./constants.js";
import { state } from "./state.js";
import { $ } from "./util.js";

export function setOutlook(outlook) {
  state.outlook = OUTLOOKS.includes(outlook) ? outlook : "short_term";
  document.querySelectorAll("[data-outlook]").forEach((button) => {
    const selected = button.dataset.outlook === state.outlook;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-checked", String(selected));
    // Roving tabindex: one tab stop per radio group, arrows move inside it.
    button.setAttribute("tabindex", selected ? "0" : "-1");
  });
  try { localStorage.setItem(OUTLOOK_KEY, state.outlook); } catch (_) {}
  updateAdvancedSummary();
}

export function setDepth(depth) {
  state.depth = DEPTH_PROFILES[depth] ? depth : "medium";
  document.querySelectorAll("[data-depth]").forEach((button) => {
    const selected = button.dataset.depth === state.depth;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-checked", String(selected));
    button.setAttribute("tabindex", selected ? "0" : "-1");
  });
  try { localStorage.setItem(DEPTH_KEY, state.depth); } catch (_) {}
  updateAdvancedSummary();
}

// Arrow keys move selection inside each role=radiogroup the way native radio
// inputs behave; Home/End jump to the first/last option (P0.3).
export function wireRadioGroups() {
  document.querySelectorAll('[role="radiogroup"]').forEach((group) => {
    group.addEventListener("keydown", (event) => {
      const steps = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      const buttons = [...group.querySelectorAll('[role="radio"]')];
      if (!buttons.length) return;
      let target = null;
      if (event.key in steps) {
        event.preventDefault();
        const current = buttons.indexOf(document.activeElement);
        target = buttons[(current + steps[event.key] + buttons.length) % buttons.length];
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        target = event.key === "Home" ? buttons[0] : buttons[buttons.length - 1];
      }
      if (target) {
        target.click();
        target.focus();
      }
    });
  });
}

// The collapsed disclosure still shows the current selections.
export function updateAdvancedSummary() {
  const values = $("adv-values");
  if (values) values.textContent = `${OUTLOOK_LABELS[state.outlook]} · ${depthProfile().label}`;
}

export function depthProfile() {
  return DEPTH_PROFILES[state.depth] || DEPTH_PROFILES.medium;
}

export function activeAgents() {
  const profile = depthProfile();
  return AGENTS.filter((agent) => {
    if (agent.stage === "Research") return profile.research.includes(agent.key);
    if (agent.rebuttal) return profile.rebuttals && state.debateRounds >= 2;
    return true;
  });
}

function agentCount(profileKey) {
  const profile = DEPTH_PROFILES[profileKey];
  return profile.research.length + 3 + (profile.rebuttals && state.debateRounds >= 2 ? 2 : 0);
}

// Agent counts on the buttons depend on the server's debate-rounds setting.
export function updateDepthLabels() {
  const labels = {
    fast: `Fast · ${agentCount("fast")} agents`,
    medium: `Medium · ${agentCount("medium")} agents`,
    expert: `Expert · ${agentCount("expert")} agents`,
  };
  document.querySelectorAll("[data-depth]").forEach((button) => {
    const small = button.querySelector("small");
    if (small && labels[button.dataset.depth]) small.textContent = labels[button.dataset.depth];
  });
}
