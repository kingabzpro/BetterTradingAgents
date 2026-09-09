/* Per-ticker follow-up chat with the portfolio manager. */

import { state } from "./state.js";
import { $, escapeAttr, escapeHtml, showToast } from "./util.js";

export function chatState(ticker) {
  if (!state.chats.has(ticker)) {
    state.chats.set(ticker, { messages: [], busy: false, open: false });
  }
  return state.chats.get(ticker);
}

export function toggleChat(ticker) {
  const chat = chatState(ticker);
  setChatOpen(ticker, !chat.open);
}

export function setChatOpen(ticker, open) {
  const chat = chatState(ticker);
  chat.open = open;
  const panel = $(`chat-panel-${ticker}`);
  const toggle = $(`chat-toggle-${ticker}`);
  if (!panel || !toggle) return;
  // If focus sits inside the panel (e.g. the message box), return it to the
  // control that opened the chat instead of dropping it to the page (P0.3).
  const returnFocus = !open && panel.contains(document.activeElement);
  panel.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  if (returnFocus) toggle.focus();
  if (open) {
    renderChatMessages(ticker);
    const input = $(`chat-input-${ticker}`);
    if (input && !chat.busy) input.focus();
  }
}

function renderChatMessages(ticker) {
  const chat = chatState(ticker);
  const list = $(`chat-msgs-${ticker}`);
  if (!list) return;
  const bubble = (message) => `
    <div class="chat-msg ${message.role === "user" ? "user" : "assistant"}">
      <span class="chat-author">${message.role === "user" ? "You" : "Portfolio Manager"}</span>
      <p>${escapeHtml(message.content)}</p>
    </div>`;
  const typing = chat.busy
    ? '<div class="chat-msg assistant"><span class="chat-author">Portfolio Manager</span><p class="chat-typing" aria-label="Manager is answering"><span></span><span></span><span></span></p></div>'
    : "";
  const markup = chat.messages.map(bubble).join("") + typing;
  list.innerHTML = markup
    || '<p class="chat-empty">Ask about the decision, the evidence behind it, or how this ticker fits your own plans.</p>';
  list.scrollTop = list.scrollHeight;
}

export async function sendChatMessage(ticker) {
  const chat = chatState(ticker);
  const input = $(`chat-input-${ticker}`);
  const send = $(`chat-send-${ticker}`);
  if (chat.busy || !input || !state.runId) return;
  const content = input.value.trim();
  if (!content) return;
  chat.messages.push({ role: "user", content });
  input.value = "";
  chat.busy = true;
  input.disabled = true;
  send.disabled = true;
  renderChatMessages(ticker);
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, messages: chat.messages.slice(-20) }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `failed (${response.status})`);
    chat.messages.push({ role: "assistant", content: payload.answer });
  } catch (error) {
    // Put the question back so it can be edited and resent.
    chat.messages.pop();
    input.value = content;
    showToast(`Could not get an answer: ${error.message}`);
  } finally {
    chat.busy = false;
    input.disabled = false;
    send.disabled = false;
    renderChatMessages(ticker);
    input.focus();
  }
}
