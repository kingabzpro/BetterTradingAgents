/* Static definitions: agent metadata, outlook/depth profiles, storage keys. */

export const ICONS = {
  technical: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12.5 6 8l2.5 2.5L13.5 4"/></svg>',
  fundamental: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3.5h10M3 7h10M3 10.5h6"/><circle cx="12.4" cy="10.7" r="1.6"/></svg>',
  news: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="5.7"/><path d="M2.3 8h11.4M8 2.3c-1.8 1.6-2.7 3.5-2.7 5.7s.9 4.1 2.7 5.7c1.8-1.6 2.7-3.5 2.7-5.7S9.8 3.9 8 2.3z"/></svg>',
  sentiment: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 8a5.5 5.5 0 0 1-8.1 4.8L2.5 13.5l.7-2.9A5.5 5.5 0 1 1 13.5 8z"/><path d="M5.8 7.2h.01M8 7.2h.01M10.2 7.2h.01" stroke-width="2"/></svg>',
  forecast: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12.5 6 9l2.5 2.5L12 8"/><path d="M12 8l2.3-2.3" stroke-dasharray="1.5 1.3"/><path d="M12.2 5.7h2.1v2.1"/></svg>',
  bull: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 12.5 12.5 3.5M6.5 3.5h6v6"/></svg>',
  bear: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3.5l9 9M12.5 6.5v6h-6"/></svg>',
  bull_rebuttal: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 12.5 12.5 3.5M6.5 3.5h6v6"/><path d="M2.5 5.5h3M2.5 8h2"/></svg>',
  bear_rebuttal: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3.5l9 9M12.5 6.5v6h-6"/><path d="M2.5 5.5h3M2.5 8h2"/></svg>',
  manager: '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5" width="11" height="8" rx="2"/><path d="M6 5V3.6A1.6 1.6 0 0 1 7.6 2h.8A1.6 1.6 0 0 1 10 3.6V5M2.5 8.5h11"/></svg>',
};

export const AGENTS = [
  { key: "technical", label: "Technical", stage: "Research" },
  { key: "fundamental", label: "Fundamentals", stage: "Research" },
  { key: "news", label: "News", stage: "Research" },
  { key: "sentiment", label: "Sentiment", stage: "Research" },
  { key: "forecast", label: "Forecast", stage: "Research" },
  { key: "bull", label: "Bull", stage: "Debate" },
  { key: "bear", label: "Bear", stage: "Debate" },
  { key: "bull_rebuttal", label: "Bull rebuttal", stage: "Debate", rebuttal: true },
  { key: "bear_rebuttal", label: "Bear rebuttal", stage: "Debate", rebuttal: true },
  { key: "manager", label: "Portfolio manager", stage: "Decision" },
];

export const RESEARCH_KEYS = ["technical", "fundamental", "news", "sentiment", "forecast"];

export const LAST_RUN_KEY = "bta:lastRunId";
export const CLIENT_ID_KEY = "bta:clientId";
export const TICKER_PATTERN = /^[A-Z0-9.\-]{1,10}$/;
// Mirrors STREAM_WINDOW_CHARS in app/workflow.py: the reasoning pane keeps the
// tail of each agent's token stream (the server caps the total it forwards).
export const STREAM_WINDOW_CHARS = 2048;
export const OUTLOOKS = ["day_trade", "short_term", "long_term"];
export const OUTLOOK_LABELS = { day_trade: "Day trading", short_term: "Short term", long_term: "Long term" };
export const ADVANCED_OPEN_KEY = "bta:advancedOpen";
export const OUTLOOK_KEY = "bta:outlook";
export const DEPTH_KEY = "bta:depth";
export const DEPTH_PROFILES = {
  fast: { label: "Fast", research: ["technical", "news"], rebuttals: false },
  medium: { label: "Medium", research: ["technical", "fundamental", "news", "forecast", "sentiment"], rebuttals: false },
  expert: { label: "Expert", research: ["technical", "fundamental", "news", "forecast", "sentiment"], rebuttals: true },
};
export const EVIDENCE_META = {
  technical: { title: "Technical" },
  fundamental: { title: "Fundamentals" },
  news: { title: "News" },
  sentiment: { title: "Sentiment" },
  forecast: { title: "Forecast" },
};
