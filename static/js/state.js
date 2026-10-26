/* Page-level mutable state for the analysis screen. */

export const state = {
  running: false,
  discovering: false,
  finishing: false,
  es: null,
  runId: null,
  incognito: false, // toggle state: applies to the next run
  runIncognito: false, // the current/last run's mode, frozen at start
  tickers: new Map(),
  runStartedAtMs: null,
  timer: null,
  debateRounds: 1,
  streamWarningShown: false,
  tickerTags: [],
  maxTickers: 5,
  outlook: "short_term",
  depth: "fast",
  chats: new Map(), // ticker -> { messages: [{role, content}], busy: false, open: false }
};
