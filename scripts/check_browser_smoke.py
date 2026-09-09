"""Automated browser smoke test for the core journey (ROADMAP P0.3).

Drives the real FastAPI app and the real frontend in a system Chromium
(Microsoft Edge, then Chrome; no browser download required) with the LLM and
market-data layers swapped for deterministic fakes. Covers the keyboard-only
journey, focus behavior, live-region setup, accessible names, and reflow at
320 px / 640 px (200% zoom at a 1280 px screen).

Run: uv run python -m scripts.check_browser_smoke
Skips with exit 0 when playwright or a system Chromium is unavailable; every
other failure is real. The manual screen-reader protocol lives in
docs/ACCESSIBILITY.md.
"""

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import urllib.request

# Isolate state before app modules read their configuration.
_DB = Path(tempfile.mkdtemp()) / "browser_smoke_test.db"
os.environ["DB_PATH"] = str(_DB)

import uvicorn  # noqa: E402

from app import memory, runs  # noqa: E402
from app.main import app  # noqa: E402
from app.models import StockAnalysis  # noqa: E402

TICKER = "SMKE"
MANUAL_TICKER = "XOM"
BASE_URL = ""  # set once the server is up


async def fake_analyze(ticker: str, emit, run_id: str = "", **_kwargs) -> StockAnalysis:
    """Deterministic analysis with the real event sequence, no network."""
    await emit("ticker_started", {"ticker": ticker})
    await asyncio.sleep(0.2)
    await emit(
        "ticker_data",
        {
            "ticker": ticker,
            "price": 100.0,
            "company_name": "Smoke Test Inc",
            "sources": {"prices": "smoke"},
        },
    )
    for agent, signal in (("technical", "bullish"), ("news", "neutral"), ("manager", "bullish")):
        await emit("agent_started", {"ticker": ticker, "agent": agent})
        await asyncio.sleep(0.2)
        await emit(
            "agent_completed",
            {
                "ticker": ticker,
                "agent": agent,
                "signal": signal,
                "confidence": 0.6,
                "summary": "smoke-test evidence",
                "duration_s": 0.2,
            },
        )
    analysis = StockAnalysis(
        ticker=ticker,
        company_name="Smoke Test Inc",
        price=100.0,
        decision="BUY",
        confidence=0.62,
        summary="Smoke-test decision.",
        suggested_size_usd=5000.0,
        as_of=datetime.now(timezone.utc).isoformat(),
    )
    await memory.record_decision(run_id, analysis)
    await emit(
        "ticker_completed",
        {
            "ticker": ticker,
            "decision": analysis.decision,
            "confidence": analysis.confidence,
            "duration_s": 0.8,
            "analysis": analysis.model_dump(),
        },
    )
    return analysis


async def fake_portfolio_summary():
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server() -> tuple[uvicorn.Server, int]:
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                if response.status == 200:
                    return server, port
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("smoke-test server did not become ready")


def tab_until_focused(page, selector: str, limit: int = 30) -> None:
    for _ in range(limit):
        if page.evaluate(
            "(s) => document.activeElement instanceof Element && document.activeElement.matches(s)",
            selector,
        ):
            return
        page.keyboard.press("Tab")
    raise AssertionError(f"Tab never reached {selector}")


def assert_no_page_scroll(page, width: int, label: str) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    page.wait_for_timeout(150)
    overflow = page.evaluate(
        "document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth"
    )
    assert overflow <= 1, f"{label}: page scrolls horizontally by {overflow}px at {width}px"


def check_analysis_page(page) -> None:
    page.goto(f"{BASE_URL}/", wait_until="networkidle")
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    # Skip link: first Tab stop, visibly focused.
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.classList.contains('skip-link')"), \
        "first Tab stop should be the skip link"
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert outline != "none", "focused skip link must show a visible outline"

    # Add a ticker with the keyboard; the icon-only remove control is named.
    tab_until_focused(page, "#ticker-input")
    page.keyboard.type(TICKER)
    page.keyboard.press("Enter")
    remove = page.locator(f".tag-remove[data-remove='{TICKER}']")
    assert remove.count() == 1, "ticker tag should appear"
    assert remove.first.get_attribute("aria-label") == f"Remove {TICKER}", \
        "icon-only remove button needs a programmatic name"
    assert page.evaluate("document.activeElement.id") == "ticker-input", \
        "focus should stay in the input after adding a tag"

    # Remove it with the keyboard; focus returns to the owning control.
    tab_until_focused(page, f".tag-remove[data-remove='{TICKER}']")
    page.keyboard.press("Enter")
    assert remove.count() == 0, "tag should be gone after Enter on remove"
    assert page.evaluate("document.activeElement.id") == "ticker-input", \
        "focus must return to the ticker input after removing a tag"

    # Radio groups: one tab stop, arrows move selection (roving tabindex).
    tab_until_focused(page, "#advanced-options summary")
    page.keyboard.press("Enter")
    assert page.evaluate("document.getElementById('advanced-options').open"), \
        "Enter should open advanced options"
    tab_until_focused(page, "[data-outlook='short_term']")
    assert page.evaluate("document.activeElement.getAttribute('aria-checked')") == "true"
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.dataset.outlook") == "long_term"
    assert page.evaluate("document.activeElement.getAttribute('aria-checked')") == "true", \
        "ArrowRight should select the next outlook radio"
    assert page.evaluate(
        "document.querySelector(\"[data-outlook='short_term']\").getAttribute('tabindex')") == "-1", \
        "unselected radios must leave the tab order"
    page.keyboard.press("ArrowLeft")

    # Start the run from the keyboard; focus lands on the results heading.
    tab_until_focused(page, "#ticker-input")
    page.keyboard.type(TICKER)
    page.keyboard.press("Enter")
    tab_until_focused(page, "#analyze-btn")
    page.keyboard.press("Enter")
    page.wait_for_selector("#live-section:not(.hidden)", timeout=10_000)
    page.wait_for_selector("#results-list .result-card", timeout=20_000)
    page.wait_for_function(
        "document.activeElement && document.activeElement.id === 'results-heading'",
        timeout=10_000,
    )

    # Announcements: one polite region for the run, none per agent step.
    assert page.locator("#overall-status[aria-live='polite']").count() == 1
    assert page.evaluate(
        "document.querySelectorAll('.status[aria-live], .status[role=\"status\"]').length"
    ) == 0, "per-agent status must not be a live region"
    assert page.inner_text("#overall-status").strip() == "Analysis complete"
    assert page.get_attribute("#overall-progress", "aria-valuenow") == "100"
    assert page.evaluate(
        "[...document.querySelectorAll('button')].filter((b) => "
        "!(b.getAttribute('aria-label') || b.textContent.trim())).length === 0"
    ), "every button needs an accessible name"

    # The evidence-strength fact shows the label first, raw value second, and
    # a track-record line that is honest about a small sample (P1.1).
    page.wait_for_function(
        f"document.querySelector('#track-record-{TICKER}') "
        f"&& document.querySelector('#track-record-{TICKER}').textContent.trim().length > 0",
        timeout=10_000,
    )
    strength = page.evaluate(
        f"document.getElementById('track-record-{TICKER}').parentElement.querySelector('strong').textContent"
    )
    assert strength.startswith(("Low evidence", "Moderate evidence", "Strong evidence")), \
        "evidence-strength label must be primary, raw value secondary"
    track_record = page.inner_text(f"#track-record-{TICKER}")
    assert "Track record unavailable" in track_record or "n=" in track_record, \
        "track record must always state its sample size"

    # Chat panel: Enter opens and focuses the input, Escape closes and
    # returns focus to the toggle.
    tab_until_focused(page, f"#chat-toggle-{TICKER}")
    page.keyboard.press("Enter")
    assert page.evaluate(f"document.activeElement.id === 'chat-input-{TICKER}'"), \
        "opening the chat should focus its input"
    page.keyboard.press("Escape")
    assert page.evaluate(f"document.getElementById('chat-panel-{TICKER}').hidden"), \
        "Escape should close the chat panel"
    assert page.evaluate(f"document.activeElement.id === 'chat-toggle-{TICKER}'"), \
        "Escape must return focus to the chat toggle"

    # Evidence disclosure: Enter expands, Escape collapses and refocuses.
    tab_until_focused(page, ".result-summary")
    page.keyboard.press("Enter")
    assert page.get_attribute(".result-summary", "aria-expanded") == "true"
    page.keyboard.press("Escape")
    assert page.get_attribute(".result-summary", "aria-expanded") == "false"
    assert page.evaluate(
        "document.activeElement.classList.contains('result-summary')"
    ), "Escape must return focus to the result summary"

    # Summary-table action opens the same card and moves focus into it.
    page.click(".table-open-btn")
    assert page.get_attribute(".result-summary", "aria-expanded") == "true"
    assert page.evaluate(
        "document.activeElement.classList.contains('result-summary')"
    ), "View evidence should focus the result summary"

    # BUY offers the demo-portfolio add; the confirmation note is a live region.
    page.click(f"#add-{TICKER}")
    page.wait_for_selector(f"#added-{TICKER}:not(.hidden)", timeout=10_000)
    assert page.locator(f"#added-{TICKER}[role='status']").count() == 1

    assert not page_errors, f"page raised JS errors: {page_errors}"
    assert_no_page_scroll(page, 320, "analysis page")
    assert_no_page_scroll(page, 640, "analysis page")
    page.set_viewport_size({"width": 1280, "height": 900})


def check_portfolio_page(page) -> None:
    page.goto(f"{BASE_URL}/portfolio", wait_until="networkidle")
    page.wait_for_selector("#positions-body tr", timeout=10_000)
    tickers = page.evaluate(
        "[...document.querySelectorAll('#positions-body strong')].map((n) => n.textContent)"
    )
    assert TICKER in tickers, "the demo position added from the results must appear"

    # Manual add with an explicit price stays offline.
    page.fill("#add-ticker", MANUAL_TICKER)
    page.fill("#add-shares", "2")
    page.fill("#add-price", "50")
    page.click("#add-holding-btn")
    page.wait_for_function(
        f"[...document.querySelectorAll('#positions-body strong')].some((n) => n.textContent === '{MANUAL_TICKER}')",
        timeout=10_000,
    )
    close_buttons = page.locator("#positions-body .close-btn")
    assert close_buttons.count() == 2, "each position needs a Close action"
    assert page.evaluate(
        "[...document.querySelectorAll('button')].filter((b) => "
        "!(b.getAttribute('aria-label') || b.textContent.trim())).length === 0"
    ), "every button needs an accessible name"
    assert_no_page_scroll(page, 320, "portfolio page")


def check_history_page(page) -> None:
    page.goto(f"{BASE_URL}/history", wait_until="networkidle")
    page.wait_for_selector(".history-run", timeout=10_000)
    rerun = page.locator(".history-rerun").first
    assert "rerun=1" in (rerun.get_attribute("href") or ""), \
        "completed runs must expose a rerun link with the original settings"
    assert_no_page_scroll(page, 320, "history page")

    # Rerun navigates home, prefills, auto-runs (cached analysis), and lands
    # focus on the results heading again.
    rerun.click()
    page.wait_for_selector("#results-list .result-card", timeout=20_000)
    page.wait_for_function(
        "document.activeElement && document.activeElement.id === 'results-heading'",
        timeout=10_000,
    )


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("BROWSER SMOKE SKIPPED: playwright is not installed (uv sync --group dev)")
        return

    server, port = start_server()
    global BASE_URL
    BASE_URL = f"http://127.0.0.1:{port}"
    original_analyze = runs.analyze_ticker
    original_portfolio = runs.fetch_portfolio_summary
    runs.analyze_ticker = fake_analyze
    runs.fetch_portfolio_summary = fake_portfolio_summary
    try:
        with sync_playwright() as playwright:
            browser = None
            for channel in ("msedge", "chrome", None):
                try:
                    browser = (
                        playwright.chromium.launch(headless=True, channel=channel)
                        if channel
                        else playwright.chromium.launch(headless=True)
                    )
                    break
                except Exception:  # noqa: BLE001 - try the next installed browser
                    continue
            if browser is None:
                print("BROWSER SMOKE SKIPPED: no system Chromium (Edge/Chrome) found")
                return
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                check_analysis_page(page)
                check_portfolio_page(page)
                check_history_page(page)
            finally:
                browser.close()
    finally:
        runs.analyze_ticker = original_analyze
        runs.fetch_portfolio_summary = original_portfolio
        server.should_exit = True
    print("BROWSER SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
