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
  });
  try { localStorage.setItem(DEPTH_KEY, state.depth); } catch (_) {}
  updateAdvancedSummary();
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
