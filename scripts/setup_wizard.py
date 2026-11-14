"""Interactive setup TUI (run `uv run setup` after `uv sync`).

Arrow-key prompts (questionary): pick an LLM provider, then tick the
integrations you want from one checkbox. The LLM key and the Alpaca paper
account are live-tested before they can save as connected (a bad LLM key
means silent mock mode and a bad paper key means dead trading - worth one
cheap call each); every other key gets a format check and is validated at
first use, because each of those providers has a documented runtime
fallback. Nothing is written until the final summary is confirmed; values
save through the app settings store (secrets to the OS keychain, applied
live). When questionary is missing or stdin is not a terminal, the same
flow runs on numbered plain-text prompts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from app import broker, settings_store
from app.config import LLM_DEFAULT_BASE_URL, LLM_DEFAULT_MODEL, settings

try:  # optional at import time; the plain UI covers a missing install
    import questionary
except ImportError:  # pragma: no cover - exercised only outside the venv
    questionary = None

# ------------------------------------------------------------- text styling --

_DIM, BOLD, CYAN, GREEN, YELLOW, RED, MAGENTA = (
    "\033[2m", "\033[1m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[35m",
)
_RESET = "\033[0m"


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("BTA_WIZARD_COLOR") == "1":
        return True
    return sys.stdout.isatty()


_COLOR = _use_color()
if _COLOR and os.name == "nt":
    os.system("")  # enable ANSI on legacy Windows consoles


def paint(text: str, *codes: str) -> str:
    return "".join(codes) + text + _RESET if _COLOR else text


def ok(text: str) -> str:
    return paint(f"  ✓ {text}", GREEN, BOLD)


def warn(text: str) -> str:
    return paint(f"  ⚠ {text}", YELLOW)


def fail(text: str) -> str:
    return paint(f"  ✗ {text}", RED, BOLD)


def divider() -> str:
    return paint("  " + "─" * 58, _DIM)


def header(step: int, total: int, title: str) -> None:
    print()
    print(divider())
    print(paint(f"  Step {step} of {total}  ", BOLD, CYAN) + paint(f"· {title}", BOLD))
    print(divider())


# ------------------------------------------------------------------ the UI --

PROVIDERS = [
    {"key": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-6-luna"},
    {"key": "zai", "label": "Z.AI (GLM)", "base_url": "https://api.z.ai/api/paas/v4", "model": "glm-5.3-flash"},
    {"key": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash"},
    {"key": "openrouter", "label": "OpenRouter (many models, one key)", "base_url": "https://openrouter.ai/api/v1", "model": "zai-org/glm-5.3-flash"},
    {"key": "custom", "label": "Any other OpenAI-compatible endpoint (vLLM, llama.cpp, proxy...)", "base_url": "", "model": ""},
]

HOST_LABELS = {
    "api.openai.com": "OpenAI",
    "api.z.ai": "Z.AI (GLM)",
    "api.deepseek.com": "DeepSeek",
    "openrouter.ai": "OpenRouter",
}

# The optional integrations, in checkbox order. `field` is the settings-store
# secret; Alpaca carries two secrets plus a live account test, so it runs
# through its own branch.
INTEGRATIONS: list[dict[str, Any]] = [
    {"key": "finnhub", "label": "Finnhub", "role": "market data: fundamentals + company news", "field": "finnhub_api_key"},
    {"key": "olostep", "label": "Olostep", "role": "web search + scraping, social sentiment", "field": "olostep_api_key"},
    {"key": "tavily", "label": "Tavily", "role": "web search (first fallback behind Olostep)", "field": "tavily_api_key"},
    {"key": "firecrawl", "label": "Firecrawl", "role": "web search (second fallback behind Olostep)", "field": "firecrawl_api_key"},
    {"key": "bako", "label": "Bako", "role": "time forecasting: hosted TimesFM predictions", "field": "bako_api_key"},
    {"key": "alpaca", "label": "Alpaca paper trading", "role": "paper account and autopilot", "field": None},
]


class QuestionaryUI:
    """Arrow-key TUI. select/checkbox return values, never titles."""

    def select(self, message: str, choices: list[tuple[str, str]]) -> str:
        from questionary import Choice

        value = questionary.select(
            message, choices=[Choice(title, value=value) for title, value in choices]
        ).ask()
        if value is None:  # Ctrl+C inside the prompt
            raise KeyboardInterrupt
        return value

    def checkbox(self, message: str, choices: list[tuple[str, str]]) -> list[str]:
        from questionary import Choice

        values = questionary.checkbox(
            message, choices=[Choice(title, value=value) for title, value in choices]
        ).ask()
        if values is None:
            raise KeyboardInterrupt
        return values

    def confirm(self, message: str, default: bool = False) -> bool:
        answer = questionary.confirm(message, default=default).ask()
        if answer is None:
            raise KeyboardInterrupt
        return answer

    def text(self, message: str, default: str = "") -> str:
        answer = questionary.text(message, default=default).ask()
        if answer is None:
            raise KeyboardInterrupt
        return answer.strip()

    def secret(self, message: str) -> str:
        import getpass

        return getpass.getpass(f"{message} (input hidden): ").strip()


class PlainUI:
    """Numbered prompts: the no-questionary / piped-stdin fallback."""

    def select(self, message: str, choices: list[tuple[str, str]]) -> str:
        print(f"\n  {message}")
        for index, (title, _value) in enumerate(choices, start=1):
            print(f"   {index}) {title}")
        raw = input("  Choose a number: ").strip()
        try:
            return choices[int(raw) - 1][1]
        except (ValueError, IndexError):
            return choices[0][1]

    def checkbox(self, message: str, choices: list[tuple[str, str]]) -> list[str]:
        print(f"\n  {message} (any number of them; plain Enter skips all)")
        for index, (title, _value) in enumerate(choices, start=1):
            print(f"   {index}) {title}")
        raw = input("  Numbers, space or comma separated: ").strip()
        picked: list[str] = []
        for token in raw.replace(",", " ").split():
            if token.isdigit():
                index = int(token) - 1
                if 0 <= index < len(choices):
                    picked.append(choices[index][1])
        return picked

    def confirm(self, message: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        raw = input(f"  {message} ({hint}): ").strip().lower()
        return (default if not raw else raw in ("y", "yes"))

    def text(self, message: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        raw = input(f"  {message}{suffix}: ").strip()
        return raw or default

    def secret(self, message: str) -> str:
        # getpass on Windows waits on the console even when stdin is a pipe
        # (hangs forever in piped/CI runs), so hide input only on a real tty.
        if sys.stdin.isatty():
            import getpass

            try:
                return getpass.getpass(f"{message} (input hidden): ").strip()
            except (EOFError, OSError):
                return ""
        return input(f"{message}: ").strip()


def make_ui() -> Any:
    if questionary is not None and sys.stdin.isatty():
        return QuestionaryUI()
    return PlainUI()


# ------------------------------------------------------------- live API tests --

def test_llm(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """One-token POST to {base_url}/chat/completions."""
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return True, f"HTTP {response.status}, endpoint and key work"
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return False, f"HTTP {error.code}: the endpoint answered but rejected this key"
        if error.code == 404:
            return False, "HTTP 404: check the base URL and model name"
        return False, f"HTTP {error.code}: {error.reason}"
    except OSError as error:
        return False, f"could not reach the endpoint ({error})"


def test_alpaca(key_id: str, secret: str) -> tuple[bool, str]:
    """One paper-account read through the app's own broker seam."""
    saved = (settings.alpaca_api_key_id, settings.alpaca_api_secret_key)
    settings.alpaca_api_key_id, settings.alpaca_api_secret_key = key_id, secret
    try:
        account = broker._client().get_account()
        return True, f"paper account {account.account_number} ({account.status or 'status unknown'})"
    except Exception as error:  # noqa: BLE001 - the failure text is the point
        return False, str(error)[:200]
    finally:
        settings.alpaca_api_key_id, settings.alpaca_api_secret_key = saved


TESTS: dict[str, Callable[..., tuple[bool, str]]] = {
    "llm": test_llm,
    "alpaca": test_alpaca,
}


# ----------------------------------------------------------------- helpers --

def mask(secret: str) -> str:
    return f"{secret[:4]}...{secret[-2:]}" if len(secret) > 8 else "(set)"


def provider_label(base_url: str) -> str:
    host = urllib.parse.urlparse(base_url).netloc.replace("www.", "")
    return HOST_LABELS.get(host, host or "custom endpoint")


def key_from(name: str) -> str:
    """Use the resolved file/environment value before a keychain fallback."""
    return str(getattr(settings, name) or "") or settings_store.keychain_get(name) or ""


# Defaults come straight from config so "already configured" is claimed
# only for explicitly set values. Without this, a keychain key plus
# untouched defaults would present as "OpenAI is already configured",
# which is a lie the live test then exposes.
def llm_explicitly_set() -> bool:
    """True only when base URL or model differs from the app defaults."""
    base_url = str(settings.llm_base_url or "").strip()
    model = str(settings.llm_model or "").strip()
    return base_url not in ("", LLM_DEFAULT_BASE_URL) or model not in (
        "",
        LLM_DEFAULT_MODEL,
    )


def safe_base_url(url: str) -> bool:
    """Mirror the app's P2.11 guard: a plain-http remote URL leaks the key."""
    if not url.startswith("http://"):
        return True
    host = urllib.parse.urlparse(url).netloc
    return host.startswith(("localhost", "127.0.0.1"))


def looks_like_key(value: str) -> bool:
    """Light format gate for providers that validate lazily at first use."""
    return len(value) >= 8 and not any(char.isspace() for char in value)


class SetupState:
    """What this run decided: pending values + per-section summary rows."""

    def __init__(self) -> None:
        self.pending: dict[str, str] = {}  # settings-store field name -> value
        self.status: dict[str, tuple[str, str]] = {}  # section -> (ok|kept|warn, detail)

    def mark(self, section: str, kind: str, detail: str) -> None:
        self.status[section] = (kind, detail)

    def has(self, field: str) -> bool:
        return field in self.pending or bool(key_from(field))


def _enter_key_light(ui: Any, label: str) -> str | None:
    """Paste a key for a lazily-validated provider; format-checked only."""
    while True:
        key = ui.secret(f"{label} API key")
        if not key:
            print(warn("Nothing entered; skipped."))
            return None
        if not looks_like_key(key):
            print(warn("That does not look like an API key (too short or contains spaces)."))
            continue
        return key


# ---------------------------------------------------------------- sections --

def step_llm(ui: Any, tests: dict[str, Callable[..., tuple[bool, str]]], state: SetupState) -> None:
    base_url = str(settings.llm_base_url or "").strip()
    model = str(settings.llm_model or "").strip()
    existing_key = key_from("llm_api_key")

    if existing_key and base_url and llm_explicitly_set():
        label = provider_label(base_url)
        print(
            f"\n  {label} is already configured with model `{paint(model or 'default', MAGENTA)}`"
            f" (key {mask(existing_key)})."
        )
        choice = ui.select(
            "Use the existing configuration?",
            [
                ("Use it (test the connection now)", "use"),
                ("Configure a different provider or model", "new"),
                ("Skip LLM configuration for now", "skip"),
            ],
        )
        if choice == "use":
            good, detail = tests["llm"](base_url, existing_key, model)
            if good:
                print(ok(f"{detail} - reusing the saved key"))
                state.mark("llm", "ok", f"{label} / {model}")
                return
            print(fail(detail))
            action = ui.select(
                "The saved key failed its test. What now?",
                [
                    ("Enter a different key", "new"),
                    ("Keep the saved key anyway (marked unverified)", "keep"),
                    ("Skip this provider for now", "skip"),
                ],
            )
            if action == "new":
                choice = "new"
            elif action == "keep":
                state.mark("llm", "kept", f"{label} / {model} - saved key unverified")
                return
            else:
                state.mark("llm", "kept", f"{label} / {model} - existing kept, test failed")
                return
        elif choice == "skip":
            state.mark("llm", "kept", f"{label} / {model} - left as configured")
            return

    while True:
        preset_index = ui.select(
            "Pick an LLM provider (arrow keys, Enter):",
            [(p["label"], str(i)) for i, p in enumerate(PROVIDERS)],
        )
        preset = PROVIDERS[int(preset_index)]
        base_url = ui.text("Base URL", preset["base_url"])
        model = ui.text("Model", preset["model"])
        if base_url and not safe_base_url(base_url):
            print(fail("Plain-http remote URLs are refused: the API key would leak. Use https://."))
            continue
        if not base_url or not model:
            print(warn("Base URL and model are both required; try again or Ctrl+C."))
            continue
        while True:
            key = ui.secret(f"{preset['label']} API key")
            if not key:
                print(warn("Nothing entered."))
                break
            good, detail = tests["llm"](base_url, key, model)
            if good:
                print(ok(detail))
                state.pending.update(
                    {"llm_base_url": base_url, "llm_model": model, "llm_api_key": key}
                )
                state.mark("llm", "ok", f"{provider_label(base_url)} / {model}")
                return
            print(fail(detail))
            action = ui.select(
                "That key did not pass. What now?",
                [
                    ("Try entering the key again", "retry"),
                    ("Pick a different provider", "again"),
                    ("Skip: run in mock mode for now", "skip"),
                ],
            )
            if action == "skip":
                state.mark("llm", "warn", "not configured - the app runs in mock mode")
                return
            if action != "retry":
                break  # back out to the provider picker


def _integration_alpaca(
    ui: Any, tests: dict[str, Callable[..., tuple[bool, str]]], state: SetupState
) -> None:
    existing_id = key_from("alpaca_api_key_id")
    existing_secret = key_from("alpaca_api_secret_key")
    if existing_id and existing_secret:
        if ui.confirm(
            f"Use the saved Alpaca paper keys ({mask(existing_id)}) and test the account now?", True
        ):
            good, detail = tests["alpaca"](existing_id, existing_secret)
            if good:
                print(ok(detail))
                state.mark("paper", "ok", "Alpaca paper")
            else:
                print(fail(detail))
                state.mark("paper", "kept", "Alpaca paper - saved keys failed the account read")
            return
    while True:
        key_id = ui.text("Alpaca paper API key ID")
        secret = ui.secret("Alpaca paper secret key")
        if key_id and secret:
            good, detail = tests["alpaca"](key_id, secret)
            if good:
                print(ok(detail))
                state.pending.update(
                    {"alpaca_api_key_id": key_id, "alpaca_api_secret_key": secret}
                )
                state.mark("paper", "ok", "Alpaca paper")
                return
            print(fail(detail))
        else:
            print(warn("Both the key ID and the secret are required."))
        action = ui.select(
            "What now?",
            [("Enter the keys again", "retry"), ("Skip paper trading for now", "skip")],
        )
        if action == "skip":
            state.mark(
                "paper", "warn", "not configured - paper trading dormant (Settings page works too)"
            )
            return


def _derive_integration_rows(state: SetupState) -> None:
    """Summary rows for the derived sections reflect the EFFECTIVE config:
    what this run saved plus whatever was already on the machine."""
    web = next(
        (
            item["label"]
            for item in INTEGRATIONS
            if item["key"] in ("olostep", "tavily", "firecrawl") and state.has(item["field"])
        ),
        None,
    )
    state.mark(
        "web",
        "ok" if web else "warn",
        web or "not configured - news falls back to Finnhub/yfinance, sentiment to the crowd proxy",
    )
    state.mark(
        "forecast", "ok", "Bako (TimesFM)" if state.has("bako_api_key") else "Local"
    )
    state.mark(
        "market", "ok", "yfinance + Finnhub" if state.has("finnhub_api_key") else "yfinance"
    )
    if "paper" not in state.status:
        state.mark(
            "paper",
            "kept" if key_from("alpaca_api_key_id") else "warn",
            "existing keys kept"
            if key_from("alpaca_api_key_id")
            else "not configured - paper trading dormant (Settings page works too)",
        )


def step_integrations(
    ui: Any, tests: dict[str, Callable[..., tuple[bool, str]]], state: SetupState
) -> None:
    print(paint("\n  Every integration is optional; each has a documented fallback.", _DIM))
    print(paint("  The LLM and the Alpaca paper account are live-tested; other keys", _DIM))
    print(paint("  are format-checked and validated at first use.", _DIM))
    picked = ui.checkbox(
        "Which integrations do you want to set up?",
        [(f"{item['label']} - {item['role']}", item["key"]) for item in INTEGRATIONS],
    )
    for item in INTEGRATIONS:
        key, label, field = item["key"], item["label"], item["field"]
        if key not in picked:
            continue
        if key == "alpaca":
            _integration_alpaca(ui, tests, state)
            continue
        existing = key_from(field) if field else ""
        if existing and ui.confirm(f"Use the saved {label} key ({mask(existing)})?", True):
            print(ok(f"{label} key kept ({mask(existing)})."))
            continue
        entered = _enter_key_light(ui, label)
        if entered:
            state.pending[field] = entered
            print(ok(f"{label} key saved - validated at first use."))
    _derive_integration_rows(state)


SECTIONS: dict[str, tuple[str, Callable[..., None]]] = {
    "llm": ("LLM provider", step_llm),
    "integrations": ("Integrations", step_integrations),
}

# Fields each section owns: re-running a section (first pass or a redo from
# the summary) must drop its earlier pending values so a changed choice can
# never leave a stale key behind.
SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "llm": ("llm_base_url", "llm_model", "llm_api_key"),
    "integrations": (
        "finnhub_api_key",
        "olostep_api_key",
        "tavily_api_key",
        "firecrawl_api_key",
        "bako_api_key",
        "alpaca_api_key_id",
        "alpaca_api_secret_key",
    ),
}


# ------------------------------------------------------------------ summary --

def _summary_line(label: str, kind: str, detail: str) -> str:
    mark = {
        "ok": "✓ Connected",
        "kept": "⚠ Kept unverified",
        "warn": "⚠ Not configured",
        "fail": "✗ Connection failed",
    }
    color = GREEN if kind == "ok" else RED if kind == "fail" else YELLOW
    return paint(f"  {label}: {detail}  {mark.get(kind, kind)}", color)


def step_summary(ui: Any, state: SetupState) -> str:
    print()
    print(divider())
    print(paint("  Configuration summary", BOLD, CYAN))
    print(divider())
    rows = [
        ("LLM provider", "llm"),
        ("Web search + scraping", "web"),
        ("Forecasting", "forecast"),
        ("Market data", "market"),
        ("Paper trading", "paper"),
    ]
    for label, key in rows:
        kind, detail = state.status.get(key, ("warn", "not configured"))
        print(_summary_line(label, kind, detail))
    return ui.select(
        "\nSave this configuration?",
        [
            ("Save and finish", "save"),
            ("Go back and change a section", "change"),
            ("Cancel without saving anything", "cancel"),
        ],
    )


# --------------------------------------------------------------------- main --

def banner() -> None:
    print()
    print(paint("  ┌──────────────────────┬──────────────────────┐", CYAN))
    print(paint("  │ BetterTradingAgents  │   first-run setup    │", BOLD, CYAN))
    print(paint("  └──────────────────────┴──────────────────────┘", CYAN))
    print(paint("  Arrow keys + Enter to answer, space to tick. The LLM and the", _DIM))
    print(paint("  paper account are live-tested; other keys validate at first use.", _DIM))
    print(paint("  Nothing is written until you confirm the summary. Ctrl+C is safe.", _DIM))


def run_section(key: str, ui: Any, tests: dict[str, Any], state: SetupState) -> None:
    """Run one section from a clean slate for its own fields."""
    for field in SECTION_FIELDS[key]:
        state.pending.pop(field, None)
    if key == "integrations":
        # Derived rows recompute from the final effective config, so a redo
        # must forget the alpaca verdict from the previous pass.
        state.status.pop("paper", None)
    SECTIONS[key][1](ui, tests, state)


def main(argv: list[str] | None = None, ui: Any = None, tests: dict[str, Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BetterTradingAgents setup wizard")
    parser.parse_args(argv)
    ui = ui or make_ui()
    tests = tests or TESTS

    settings_store.apply_overrides()  # same initialization startup does
    state = SetupState()
    order = list(SECTIONS)
    banner()
    for number, key in enumerate(order, start=1):
        header(number, len(order), SECTIONS[key][0])
        run_section(key, ui, tests, state)

    while True:
        action = step_summary(ui, state)
        if action == "save":
            break
        if action == "cancel":
            print(fail("Setup cancelled; nothing was written."))
            return 0
        redo = ui.select(
            "Which section?",
            [(label, key) for key, (label, _step) in SECTIONS.items()],
        )
        header(0, len(order), f"Redo: {SECTIONS[redo][0]}")
        run_section(redo, ui, tests, state)

    if state.pending:
        settings_store.save(values=state.pending, clear_secrets=[])
        print(ok(f"Saved {len(state.pending)} value(s): secrets to the OS keychain, settings applied live."))
    else:
        print(ok("Nothing new to save; the current configuration stays as is."))
    print()
    print(divider())
    for line in (
        "uv run app                               # start the app",
        "open http://127.0.0.1:8000               # analyze your first tickers",
        "uv run test                              # fast offline check suite",
        "Settings page                            # change anything later, no restart",
    ):
        print(paint("  " + line, CYAN))
    print(divider())
    print(paint("  ✔ Setup complete. Happy researching!", GREEN, BOLD))
    print(paint("  Educational simulation, not investment advice.", _DIM))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n" + fail("Setup cancelled; nothing was written."))
        sys.exit(130)
