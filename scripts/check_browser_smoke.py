"""Automated browser smoke test for the core journey (ROADMAP P0.3).

Drives the real FastAPI app and the real frontend in a system Chromium
(Microsoft Edge, then Chrome; no browser download required) with the LLM and
market-data layers swapped for deterministic fakes. Covers the keyboard-only
journey, focus behavior, live-region setup, accessible names, and reflow at
320 px / 640 px (200% zoom at a 1280 px screen), plus the P1.6 filter, sort,
download, and print-brief behavior on the runs, portfolio, and analysis pages.

Run: uv run python -m scripts.check_browser_smoke
Skips with exit 0 when playwright or a system Chromium is unavailable; every
other failure is real. The manual screen-reader protocol lives in
the wiki Accessibility page.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import urllib.request

# Isolate state before app modules read their configuration. Alpaca keys are
# cleared too: the smoke must never reach the broker, even when a developer's
# .env carries real paper keys.
_DB = Path(tempfile.mkdtemp()) / "browser_smoke_test.db"
os.environ["DB_PATH"] = str(_DB)
for _var in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_TRADING_ENABLED"):
    os.environ[_var] = ""

import uvicorn  # noqa: E402

from app import main as main_module  # noqa: E402
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


async def fake_closes_between(ticker: str, start: str, end: str) -> dict[str, float]:
    """Deterministic dated closes so the price-context chart renders offline."""
    return {"2026-04-01": 95.0, "2026-06-01": 99.5, "2026-08-01": 102.0}


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

    # Price context (P1.5): the chart loads with the panel and every mark is
    # also stated as text outside the graphic.
    page.wait_for_selector(".chart-summary", timeout=10_000)
    summary = page.text_content(".chart-summary") or ""
    assert "Six months to" in summary, f"chart summary missing range: {summary}"
    assert "Analysis price $100.00" in summary, f"chart summary missing analysis mark: {summary}"
    assert "Current $102.00" in summary, f"chart summary missing current price: {summary}"
    assert page.locator("svg.price-chart[role='img']").count() == 1, \
        "the chart must expose an accessible name"

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


def check_compare_page(page) -> None:
    # Two completed runs of the same ticker exist by now; the rerun was a
    # cache hit whose result carries the deterministic `What changed` diff.
    page.goto(f"{BASE_URL}/history", wait_until="networkidle")
    client_id = page.evaluate("localStorage.getItem('bta:clientId')") or ""
    request = urllib.request.Request(
        f"{BASE_URL}/api/runs?limit=10", headers={"X-Client-ID": client_id}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        history_runs = json.load(response)
    assert len(history_runs) >= 2, "the smoke journey should leave at least two runs"
    new_run, old_run = history_runs[0]["run_id"], history_runs[1]["run_id"]

    page.goto(
        f"{BASE_URL}/compare?items={new_run}:{TICKER},{old_run}:{TICKER}",
        wait_until="networkidle",
    )
    page.wait_for_selector(".compare-col", timeout=10_000)
    assert page.locator(".compare-col").count() == 2, "two items must render two columns"
    content = page.content()
    for label in ("Final call", "Manager call", "Evidence strength", "Analyst split",
                  "Price", "5-day forecast", "Data age", "Risk flags", "Suggested size"):
        assert label in content, f"comparison row missing: {label}"
    assert "What changed" in content, "repeat analyses must lead with What changed"
    assert "Decision unchanged" in content, "the repeat-run diff must be shown"
    assert page.evaluate(
        "[...document.querySelectorAll('button')].filter((b) => "
        "!(b.getAttribute('aria-label') || b.textContent.trim())).length === 0"
    ), "every button needs an accessible name"

    # Keyboard reorder: move the second column left, the URL follows.
    tab_until_focused(page, ".compare-col:nth-child(2) .move-btn")
    page.keyboard.press("Enter")
    assert page.evaluate("new URLSearchParams(location.search).get('items')") == \
        f"{old_run}:{TICKER},{new_run}:{TICKER}", "reorder must update the shareable URL"
    page.wait_for_function(
        "(run) => document.querySelector('.compare-col')?.textContent.includes(run)",
        arg=old_run,
        timeout=10_000,
    )

    # Keyboard remove: one column left, still in the URL.
    tab_until_focused(page, ".compare-col .remove-btn")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "document.querySelectorAll('.compare-col').length === 1", timeout=10_000
    )
    assert page.evaluate("new URLSearchParams(location.search).get('items')") == \
        f"{new_run}:{TICKER}"

    # Add the other run back through the form, keyboard-activated.
    page.select_option("#add-run", old_run)
    page.select_option("#add-ticker", TICKER)
    tab_until_focused(page, "#add-compare")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "document.querySelectorAll('.compare-col').length === 2", timeout=10_000
    )

    assert_no_page_scroll(page, 1440, "compare page")
    assert_no_page_scroll(page, 320, "compare page")
    page.set_viewport_size({"width": 1280, "height": 900})


def check_p1_6_filters_and_exports(page) -> None:
    """Search, filters, sort, chips, downloads, and the print brief (P1.6)."""
    # ---- runs page: instant client-side filtering -------------------------
    page.goto(f"{BASE_URL}/history", wait_until="networkidle")
    page.wait_for_selector(".history-run", timeout=10_000)
    page.evaluate(
        "window.__fetches = 0; window.__fetchArgs = []; const orig = window.fetch; "
        "window.fetch = (...args) => { window.__fetches += 1; window.__fetchArgs.push(String(args[0])); return orig(...args); }; 'instrumented'"
    )
    total_runs = page.locator(".history-run").count()

    # A search with no matches shows the no-match state, not the empty account.
    page.fill("#run-search", "ZZZZ")
    page.wait_for_selector("#history-no-match:not(.hidden)", timeout=5_000)
    assert page.locator(".history-run").count() == 0, "no-match search must hide every card"
    assert page.locator("#history-empty.hidden").count() == 1, \
        "the no-saved-runs state must stay hidden while runs exist"
    assert page.evaluate("new URLSearchParams(location.search).get('q')") == "ZZZZ", \
        "filter state must live in the URL"
    assert "Search: ZZZZ" in page.inner_text("#filter-chips"), "active filters must appear as chips"

    # Filtering never round-trips to the server.
    assert page.evaluate("window.__fetches") == 0, \
        f"client-side filtering must not call fetch: {page.evaluate('window.__fetchArgs')}"

    # Searching the smoke ticker keeps exactly the runs that contain it.
    page.fill("#run-search", TICKER)
    page.wait_for_function(
        f"document.querySelectorAll('.history-run').length === {total_runs}", timeout=5_000
    )
    assert page.evaluate("window.__fetches") == 0

    # Keyboard: removing the chip clears the filter and keeps focus in the bar.
    tab_until_focused(page, ".chip-remove")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "new URLSearchParams(location.search).get('q') === null", timeout=5_000
    )
    assert page.locator(".chip-remove").count() == 0, "chip must disappear with its filter"
    assert page.evaluate("document.activeElement.id") == "run-search", \
        "removing the last chip must return focus to the search input"

    # Sort flips the list order without a fetch.
    first_newest = page.evaluate("document.querySelector('.history-run-id').textContent")
    page.select_option("#run-sort", "oldest")
    first_oldest = page.evaluate("document.querySelector('.history-run-id').textContent")
    assert first_newest != first_oldest, "oldest-first sort must reorder the cards"
    assert page.evaluate("new URLSearchParams(location.search).get('sort')") == "oldest"

    # Named controls: chips and export buttons render dynamically.
    assert page.evaluate(
        "[...document.querySelectorAll('button')].filter((b) => "
        "!(b.getAttribute('aria-label') || b.textContent.trim())).length === 0"
    ), "every button needs an accessible name"

    # Mobile: the toolbar collapses into a labeled disclosure that keeps the
    # filter count visible.
    page.set_viewport_size({"width": 320, "height": 800})
    page.wait_for_timeout(150)
    drawer = page.locator("#filter-drawer")
    assert drawer.get_attribute("open") is None, "the filter drawer must collapse on mobile"
    summary_text = page.inner_text("#filter-drawer summary")
    assert "Filters" in summary_text and "0" in summary_text, \
        f"collapsed drawer must show the filter count: {summary_text!r}"
    assert_no_page_scroll(page, 320, "history page with filters")
    page.set_viewport_size({"width": 1280, "height": 900})

    # Download one run as JSON; the file must match the run detail API.
    first_id = page.evaluate(
        "document.querySelector('[data-export]').dataset.export"
    )
    with page.expect_download() as download_info:
        page.locator("[data-export]").first.click()
    download = download_info.value
    assert download.suggested_filename == f"bta-run-{first_id}.json"
    payload = json.loads(Path(download.path()).read_text(encoding="utf-8"))
    assert payload["run_id"] == first_id, "the JSON download must match the run"

    # ---- portfolio page: search, sort, direction, CSV ----------------------
    page.goto(f"{BASE_URL}/portfolio", wait_until="networkidle")
    page.wait_for_selector("#positions-body tr", timeout=10_000)

    page.select_option("#pos-sort", "ticker")
    tickers = page.evaluate(
        "[...document.querySelectorAll('#positions-body strong')].map((n) => n.textContent)"
    )
    assert tickers == sorted(tickers), f"ticker sort must sort ascending: {tickers}"
    assert page.evaluate("new URLSearchParams(location.search).get('sort')") == "ticker"
    page.click("#pos-sort-dir")
    tickers_desc = page.evaluate(
        "[...document.querySelectorAll('#positions-body strong')].map((n) => n.textContent)"
    )
    assert tickers_desc == sorted(tickers, reverse=True), "direction toggle must reverse the order"
    assert page.evaluate("new URLSearchParams(location.search).get('dir')") == "desc"

    page.fill("#pos-search", MANUAL_TICKER)
    page.wait_for_function(
        f"[...document.querySelectorAll('#positions-body strong')].map((n) => n.textContent)"
        f".every((t) => t === '{MANUAL_TICKER}') && "
        f"document.querySelectorAll('#positions-body tr').length === 1",
        timeout=5_000,
    )
    assert "Search: XOM" in page.inner_text("#pos-filter-chips"), "portfolio search must show a chip"

    page.fill("#pos-search", "ZZZZ")
    page.wait_for_selector("#pos-no-match:not(.hidden)", timeout=5_000)
    assert page.locator("#positions-body tr").count() == 0

    # The chip's remove button restores the full table, keyboard included.
    tab_until_focused(page, "#pos-filter-chips .chip-remove")
    page.keyboard.press("Enter")
    page.wait_for_function(
        f"document.querySelectorAll('#positions-body tr').length === {len(tickers)}",
        timeout=5_000,
    )
    assert page.evaluate("document.activeElement.id") == "pos-search"

    # CSV download matches the visible scope: header, rows, and values.
    visible = page.evaluate(
        "[...document.querySelectorAll('#positions-body tr')].map((tr) => "
        "tr.querySelector('strong').textContent)"
    )
    with page.expect_download() as csv_info:
        page.click("#export-csv")
    csv_download = csv_info.value
    assert csv_download.suggested_filename == "bta-portfolio-positions.csv"
    lines = Path(csv_download.path()).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "Ticker,Quantity,Entry price,Current price,Cost,Value,P&L,P&L %,Added", \
        f"unexpected CSV header: {lines[0]}"
    assert [line.split(",")[0] for line in lines[1:]] == visible, \
        "the CSV must contain exactly the visible rows"

    # ---- analysis page: the print brief scopes to one card ------------------
    # The runs API needs this browser's client id; read it from the page origin.
    page.goto(f"{BASE_URL}/history", wait_until="networkidle")
    client_id = page.evaluate("localStorage.getItem('bta:clientId')") or ""
    request = urllib.request.Request(
        f"{BASE_URL}/api/runs?limit=5", headers={"X-Client-ID": client_id}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        runs = json.load(response)
    target_run = runs[0]["run_id"]

    page.goto(f"{BASE_URL}/?run={target_run}", wait_until="networkidle")
    page.wait_for_selector(".result-card", timeout=10_000)
    # The print action sits in the evidence panel; expand the card first.
    page.click(".result-summary")
    page.wait_for_selector(".result-detail:not([hidden])", timeout=5_000)
    page.evaluate(
        "window.__printState = null; window.print = () => { window.__printState = {"
        "brief: document.body.classList.contains('printing-brief'), "
        "target: !!document.querySelector('.result-card.print-target'), "
        "detailOpen: !document.querySelector('.result-detail').hidden }; }; 'instrumented'"
    )
    page.click(".print-btn")
    state = page.evaluate("window.__printState")
    assert state and state["brief"] and state["target"] and state["detailOpen"], \
        f"printing must scope to the open card: {state}"
    assert not page.evaluate("document.body.classList.contains('printing-brief')"), \
        "print classes must be cleaned up after printing"
    assert page.locator(".print-stamp").count() == 0, "the print stamp is print-only"

    # Print media emulation: navigation and controls drop out, the brief stays.
    page.emulate_media(media="print")
    assert page.evaluate("getComputedStyle(document.querySelector('header')).display") == "none"
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.result-summary')).display"
    ) != "none", "the ticker and call must lead the printed brief"
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.result-summary .summary-action')).display"
    ) == "none", "the collapse toggle chrome must not print"
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.result-detail')).display"
    ) == "block", "evidence must print even when the panel was closed"
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.chat-block')).display"
    ) == "none", "chat controls must not print"
    page.evaluate(
        "document.body.classList.add('printing-brief'); "
        "document.querySelector('.result-card').classList.add('print-target')"
    )
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.search-card')).display"
    ) == "none", "printing-brief must hide everything but the target card"
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.print-target')).display"
    ) != "none", "the target card must stay visible in print"
    page.emulate_media(media="screen")
    page.evaluate(
        "document.body.classList.remove('printing-brief'); "
        "document.querySelector('.result-card').classList.remove('print-target')"
    )


def check_trading_page(page) -> None:
    """P2.1: the trading page is a first-class dormant view without keys and
    the full lifecycle with faked broker functions."""
    from app.models import BrokerAccount, BrokerOrder, BrokerPosition, BrokerStatus

    # ---- unconfigured: setup card, banner, named controls, 320 px ------------
    page.goto(f"{BASE_URL}/", wait_until="networkidle")
    assert page.locator("header nav a[href='/trading']").count() == 1, \
        "every page must offer the Trading nav link"
    page.goto(f"{BASE_URL}/trading", wait_until="networkidle")
    content = page.content()
    assert "Simulated fills; not live-trading proof." in content, "banner must disclose paper fills"
    assert page.locator("#setup-card:not(.hidden)").count() == 1, "setup card must lead when unconfigured"
    assert page.locator("#live-section.hidden").count() == 1
    assert page.evaluate(
        "[...document.querySelectorAll('button')].filter((b) => "
        "!(b.getAttribute('aria-label') || b.textContent.trim())).length === 0"
    ), "every button needs an accessible name"
    assert_no_page_scroll(page, 320, "trading page unconfigured")

    # Result cards are unaffected: no paper order button without a connection.
    client_id = page.evaluate("localStorage.getItem('bta:clientId')") or ""
    request = urllib.request.Request(
        f"{BASE_URL}/api/runs?limit=5", headers={"X-Client-ID": client_id}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        run_id = json.load(response)[0]["run_id"]
    page.goto(f"{BASE_URL}/?run={run_id}", wait_until="networkidle")
    page.wait_for_selector(".result-card", timeout=10_000)
    page.click(".result-summary")
    page.wait_for_selector(".result-detail:not([hidden])", timeout=5_000)
    assert page.locator(".paper-open-btn").count() == 0, \
        "no paper order button may render while unconfigured"
    page.set_viewport_size({"width": 1280, "height": 900})

    # ---- connected: broker functions faked, lifecycle end to end -------------
    broker_module = main_module.broker
    orders = [
        BrokerOrder(
            client_order_id="bta-smoke-open0001", run_id=run_id, ticker=TICKER,
            side="buy", notional=5000.0, status="accepted",
        ),
        BrokerOrder(
            client_order_id="bta-smoke-filled01", run_id=run_id, ticker=TICKER,
            side="buy", notional=5000.0, status="filled",
            filled_qty=12.0, filled_avg_price=95.0,
        ),
    ]
    state = {"canceled": False}

    async def fake_status():
        return BrokerStatus(configured=True, enabled=True, max_order_usd=10000)

    async def fake_account():
        return BrokerAccount(
            account_number="PA-SMOKE", status="ACTIVE", equity=101234.56,
            cash=51234.56, buying_power=202469.12, last_equity=100000.0,
        )

    async def fake_positions():
        return [
            BrokerPosition(
                symbol=TICKER, quantity=12, avg_entry_price=95.0, current_price=100.0,
                market_value=1200.0, unrealized_pl=60.0, unrealized_plpc=0.05,
            )
        ]

    async def fake_list_orders(limit: int = 50):
        return list(orders)

    async def fake_cancel_order(client_order_id: str):
        state["canceled"] = True
        for index, order in enumerate(orders):
            if order.client_order_id == client_order_id:
                orders[index] = order.model_copy(update={"status": "canceled"})
                return orders[index]
        raise broker_module.BrokerRuleError("no order", status_code=404)

    async def fake_submit_order(run_id_arg, ticker, side, notional):
        return BrokerOrder(
            client_order_id="bta-smoke-submit01", run_id=run_id_arg, ticker=ticker,
            side=side, notional=notional, status="accepted",
        )

    fakes = {
        "status": fake_status,
        "account": fake_account,
        "positions": fake_positions,
        "list_orders": fake_list_orders,
        "cancel_order": fake_cancel_order,
        "submit_order": fake_submit_order,
    }
    originals = {name: getattr(broker_module, name) for name in fakes}
    for name, fake in fakes.items():
        setattr(broker_module, name, fake)
    try:
        page.goto(f"{BASE_URL}/trading", wait_until="networkidle")
        page.wait_for_selector("#live-section:not(.hidden)", timeout=10_000)
        assert "$101,234.56" in page.inner_text("#acc-equity"), "equity must render"
        assert page.locator("#positions-body tr").count() == 1
        assert page.locator("#orders-body tr").count() == 2, "both orders must render"
        assert page.locator("#orders-body .status-chip.ok").count() == 1
        assert page.locator("#orders-body .cancel-btn").count() == 1, \
            "cancel only on the non-terminal row"
        link = page.locator("#orders-body a[href*='/?run=']").first
        assert run_id in (link.get_attribute("href") or ""), "decision link must resolve"
        assert page.evaluate(
            "[...document.querySelectorAll('button')].filter((b) => "
            "!(b.getAttribute('aria-label') || b.textContent.trim())).length === 0"
        ), "every button needs an accessible name"

        page.on("dialog", lambda dialog: dialog.accept())
        page.click("#orders-body .cancel-btn")
        page.wait_for_function(
            "document.querySelectorAll('#orders-body .cancel-btn').length === 0",
            timeout=10_000,
        )
        assert state["canceled"], "cancel must call the DELETE path"
        assert "canceled" in page.inner_text("#orders-body").lower()

        # The BUY result card now offers the paper order review step (M2 UI).
        page.goto(f"{BASE_URL}/?run={run_id}", wait_until="networkidle")
        page.wait_for_selector(".result-card", timeout=10_000)
        page.click(".result-summary")
        page.wait_for_selector(".paper-open-btn", timeout=10_000)
        page.click(".paper-open-btn")
        page.wait_for_selector(f"#paper-review-{TICKER}:not(.hidden)", timeout=10_000)
        assert page.is_disabled(f"#paper-place-{TICKER}"), \
            "place must stay disabled until the review checkbox is checked"
        page.check(f"#paper-confirm-{TICKER}")
        assert not page.is_disabled(f"#paper-place-{TICKER}")
        page.click(f"#paper-place-{TICKER}")
        page.wait_for_function(
            "(ticker) => document.querySelector(`#paper-status-${ticker}`)?.textContent.includes('accepted')",
            arg=TICKER,
            timeout=10_000,
        )
        status_line = page.inner_text(f"#paper-status-{TICKER}")
        assert "accepted" in status_line, f"placement status must show: {status_line}"
        assert "/trading" in page.content()
        assert_no_page_scroll(page, 320, "trading page connected")
        page.set_viewport_size({"width": 1280, "height": 900})
    finally:
        for name, original in originals.items():
            setattr(broker_module, name, original)


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
    original_closes = main_module.get_closes_between
    runs.analyze_ticker = fake_analyze
    runs.fetch_portfolio_summary = fake_portfolio_summary
    main_module.get_closes_between = fake_closes_between
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
                check_compare_page(page)
                check_p1_6_filters_and_exports(page)
                check_trading_page(page)
            finally:
                browser.close()
    finally:
        runs.analyze_ticker = original_analyze
        runs.fetch_portfolio_summary = original_portfolio
        main_module.get_closes_between = original_closes
        server.should_exit = True
    print("BROWSER SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
