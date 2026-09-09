# Accessibility verification

How we check the accessibility work shipped in ROADMAP P0.3, and how to
re-verify it after frontend changes. Two layers:

1. **Automated browser smoke test** (structure and behavior):
   `PYTHONPATH=. uv run python -m scripts.check_browser_smoke.py`.
   It drives the real app in a system Chromium with keyboard input only and
   asserts focus order, focus visibility, live-region wiring, accessible
   names, Escape behavior, and reflow at 320 px and 640 px.
2. **Manual screen-reader pass** (what automation cannot hear): the protocol
   below, run once per release that touches the UI.

## Manual screen-reader protocol

Expected setup: NVDA (free) with Chrome or Edge on Windows, or VoiceOver with
Safari on macOS. Start the app with `uv run uvicorn app.main:app` and keep the
screen reader speech (or braille) display active throughout. Work through each
section with the keyboard only; do not touch the mouse.

### 1. Analysis page (NVDA: `Ctrl+Alt+N` focus browser, then `Tab`)

| Step | Action | Expected |
|---|---|---|
| a | Press `Tab` from page load | "Skip to content, link" is announced and visibly focused |
| b | Tab to the Stocks input, type `NVDA`, press Enter | The tag appears; focus stays in the input |
| c | Tab to the tag's `×` button | Announced as "Remove NVDA, button" |
| d | Press Enter on it | Tag is removed; focus returns to the Stocks input, not the page |
| e | Open Advanced options, Tab into outlook | Only the selected radio is a tab stop; `ArrowRight`/`ArrowLeft` move and select day/short/long; the change is announced |
| f | Start an analysis | One announcement per finished ticker ("NVDA: BUY, 1 of 2 analyzed"), then "Analysis complete"; agent steps and token streams are **not** spoken |
| g | When the run finishes | Focus moves to the Results heading and it is announced |
| h | Tab to a result summary, press Enter | Evidence expands; expanded/collapsed state is announced |
| i | Press Escape | Detail collapses and focus returns to the result summary |
| j | Open Chat with Portfolio Manager, press Escape | Panel closes and focus returns to the chat toggle |
| k | On a BUY result, use Add to Demo Portfolio | The "added" note is announced once as a status message |

### 2. Runs page

| Step | Action | Expected |
|---|---|---|
| a | Open `/history` | "N saved runs, newest first" is announced as a status message |
| b | Navigate a run card | Ticker decisions, run state, and the partial-results sentence read in a sensible order |
| c | Activate Rerun | Returns to the analysis page, run restarts, focus ends on Results |

### 3. Portfolio page

| Step | Action | Expected |
|---|---|---|
| a | Open `/portfolio` | Summary values and both table captions ("Open portfolio positions", "Closed portfolio positions") are announced |
| b | Add a holding with the keyboard | New row appears; the toast confirms once |
| c | Choose a CSV file, then Cancel | Preview disappears and focus returns to "Choose CSV file" |
| d | Narrow the window to phone width | Tables become labeled cards; no control is unreachable |

### 4. Zoom and small screens

At 320 CSS pixels width and at 200% browser zoom (equivalent to a 640 px
viewport on a 1280 px screen): no horizontal page scrolling, every primary
action still reachable and visible, text is not clipped. The automated smoke
test checks the scroll-width invariant; read through one result card manually
to confirm nothing looks truncated.

## What was verified when

- 2026-09-09 (P0.3): automated smoke test green on Edge (Chromium) at 1280,
  640, and 320 px. The manual protocol above was authored with the P0.3
  changes; run it once with NVDA or VoiceOver before any shared demo, and
  record the result here.
