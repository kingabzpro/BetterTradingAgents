/* Page-level mutable state for the analysis screen. */

export const state = {
  running: false,
  discovering: false,
  finishing: false,
  es: null,
  runId: null,
  tickers: new Map(),
  runStartedAtMs: null,
  timer: null,
  debateRounds: 1,
  streamWarningShown: false,
  tickerTags: [],
  maxTickers: 5,
  outlook: "short_term",
  depth: "medium",
  chats: new Map(), // ticker -> { messages: [{role, content}], busy: false, open: false }
};
