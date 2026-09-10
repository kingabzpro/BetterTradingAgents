"""Interactive setup wizard for new users (run this first).

    uv run setup                                 (preferred; also installs deps)
    python scripts/setup_wizard.py               (stdlib only, no deps needed)

Asks a handful of questions, optionally tests the LLM key against the
provider, and writes .env from .env.example. Nothing is written until the
final confirmation, so Ctrl+C at any prompt is safe. Existing values in
.env are kept by default. Uses only the standard library so it also runs
before `uv sync`.

Colors are pure ANSI: enabled on terminals, disabled automatically when
output is piped or NO_COLOR is set (https://no-color.org), and forced on
with BTA_WIZARD_COLOR=1 for previewing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
TEMPLATE_PATH = REPO_ROOT / ".env.example"

# ---------------------------------------------------------------- styling --
# ANSI codes only; no dependency. _COLOR is computed once so piped runs
# (scripts/check_setup_wizard.py) and NO_COLOR users get plain text.


class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("BTA_WIZARD_COLOR") == "1":
        return True
    return sys.stdout.isatty()


_COLOR = _use_color()
if _COLOR and os.name == "nt":
    os.system("")  # enable ANSI on legacy Windows consoles; no-op elsewhere


def paint(text: str, *codes: str) -> str:
    if not _COLOR:
        return text
    return "".join(codes) + text + Style.RESET


def ok(text: str) -> str:
    return paint(f"  ✓ {text}", Style.GREEN, Style.BOLD)


def warn(text: str) -> str:
    return paint(f"  ⚠ {text}", Style.YELLOW)


def fail(text: str) -> str:
    return paint(f"  ✗ {text}", Style.RED, Style.BOLD)


def divider() -> str:
    width = 58
    return paint("  " + "─" * width, Style.DIM)


def header(step: int, total: int, title: str) -> None:
    print()
    print(divider())
    print(paint(f"  Step {step} of {total}  ", Style.BOLD, Style.CYAN)
          + paint(f"· {title}", Style.BOLD))
    print(divider())


def banner() -> None:
    title = " BetterTradingAgents "
    tagline = " first-run setup "
    bar = "─" * 22
    print()
    print(paint(f"  ┌{bar}┬{bar}┐", Style.CYAN))
    print(paint(f"  │{title:^22}│{tagline:^22}│", Style.BOLD, Style.CYAN))
    print(paint(f"  └{bar}┴{bar}┘", Style.CYAN))
    print(paint(
        "  A few questions and you are running. Everything is optional;",
        Style.DIM,
    ))
    print(paint(
        "  anything you skip falls back to a documented default, and with no",
        Style.DIM,
    ))
    print(paint(
        "  LLM key the app runs in clearly labeled mock mode on live market",
        Style.DIM,
    ))
    print(paint(
        "  data. Ctrl+C cancels safely at any prompt.", Style.DIM,
    ))


# --------------------------------------------------------------- presets --
# Provider presets: default base URL + a good starter model. Every value is
# editable at the prompt, so a regional endpoint or another model is one
# keystroke away. Model names follow the provider docs checked 2026-09-10.
PROVIDERS = [
    {
        "key": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.6-luna",
    },
    {
        "key": "zai",
        "label": "Z.AI (GLM)",
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-5.3-flash",
    },
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
    },
    {
        "key": "qwen",
        "label": "Alibaba Cloud Qwen (DashScope)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.8-flash",
    },
    {
        "key": "openrouter",
        "label": "OpenRouter (many models, one key)",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "zai-org/glm-5.3-flash",
    },
    {
        "key": "custom",
        "label": "Any other OpenAI-compatible endpoint (vLLM, llama.cpp, proxy...)",
        "base_url": "",
        "model": "",
    },
    {
        "key": "mock",
        "label": "Skip for now - mock mode (rule-based agents, live market data)",
        "base_url": None,
        "model": None,
    },
]

DATA_PROVIDERS = [
    (
        "FINNHUB_API_KEY",
        "Finnhub",
        "company profiles, fundamentals, and company news (falls back to yfinance)",
        "https://finnhub.io",
    ),
    (
        "OLOSTEP_API_KEY",
        "Olostep",
        "news web search fallback and Reddit/StockTwits sentiment",
        "https://olostep.com",
    ),
    (
        "NIXTLA_API_KEY",
        "Nixtla TimeGPT",
        "hosted 5-day forecast (falls back to the local trend model)",
        "https://nixtla.io",
    ),
]


def ask(prompt: str, default: str = "") -> str:
    """One line of input; empty answer means the default. EOF also skips."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})", "yes" if default else "no").lower()
    return answer in ("y", "yes")


def ask_secret(prompt: str) -> str:
    """API key entry: hidden when interactive, plain input when piped."""
    if sys.stdin.isatty():
        try:
            import getpass

            return getpass.getpass(f"{prompt}: ").strip()
        except (EOFError, OSError):
            return ""
    return ask(prompt)


def mask(secret: str) -> str:
    return f"{secret[:4]}...{secret[-2:]}" if len(secret) > 8 else "(set)"


def ping_llm(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """One-token POST to /chat/completions. Returns (ok, explanation)."""
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return True, f"HTTP {response.status}, endpoint and key work"
    except urllib.error.HTTPError as error:
        detail = f"HTTP {error.code}"
        if error.code in (401, 403):
            return False, f"{detail}: the endpoint answered but rejected this key"
        if error.code == 404:
            return False, f"{detail}: check the base URL and model name"
        return False, f"{detail}: {error.reason}"
    except OSError as error:
        return False, f"could not reach the endpoint ({error})"


def apply_values(template: str, values: dict[str, str]) -> str:
    """Set KEY=value lines on the template, uncommenting '# KEY=' when present."""
    lines = template.splitlines()
    remaining = dict(values)
    for index, line in enumerate(lines):
        stripped = line.lstrip("#").strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="BetterTradingAgents setup wizard")
    parser.add_argument(
        "--out",
        default=str(ENV_PATH),
        help="env file to write (default: ./.env); used by the offline check",
    )
    args = parser.parse_args()
    out_path = Path(args.out)
    steps = 3

    banner()

    if sys.version_info < (3, 12):
        print(warn(f"Python {sys.version.split()[0]} found; the app needs >= 3.12."))

    # ---- base text: keep an existing .env, else start from the template ------
    base_text = None
    if out_path.exists():
        print(f"\n  Found an existing {paint(out_path.name, Style.MAGENTA)}.")
        if ask_yes_no("  Keep its current values and only fill what is missing?", True):
            base_text = out_path.read_text(encoding="utf-8")
        elif not ask_yes_no("  Overwrite it from the template?", False):
            print(fail("Setup cancelled; nothing was changed."))
            return 0
    if base_text is None:
        try:
            base_text = TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError:
            print(fail(f"{TEMPLATE_PATH} is missing; cannot build {out_path.name}."))
            return 1

    # ---- LLM provider ---------------------------------------------------------
    header(1, steps, "LLM provider (any OpenAI-compatible endpoint)")
    for number, provider in enumerate(PROVIDERS, start=1):
        marker = paint("●", Style.CYAN) if provider["key"] == "zai" else " "
        print(f"   {marker} {number}) {provider['label']}")
    choice = ask("  Choose", "2")  # the README's recommended starter (GLM-5.3-Flash)
    try:
        provider = PROVIDERS[int(choice) - 1]
    except (ValueError, IndexError):
        provider = PROVIDERS[0]

    values: dict[str, str] = {}
    if provider["key"] == "mock":
        print(ok("Mock mode it is. You can add a key later in .env."))
    else:
        base_url = ask("  Base URL", provider["base_url"])
        model = ask("  Model", provider["model"])
        api_key = ask_secret("  API key (input hidden; Enter to skip and edit .env later)")
        values.update(
            {
                "LLM_BASE_URL": base_url,
                "LLM_MODEL": model,
                "LLM_API_KEY": api_key,
            }
        )
        if api_key and ask_yes_no("  Test the key with a one-token request now?", True):
            reachable, detail = ping_llm(base_url, api_key, model)
            print(ok(detail) if reachable else warn(detail))
            if not reachable:
                print(warn("The wizard will still save your answers; fix and retry later."))
        elif not api_key:
            print(warn("No key entered: the app will start in mock mode until you add one."))

    # ---- data providers -------------------------------------------------------
    header(2, steps, "Market-data providers (all optional; each has a fallback)")
    for env_key, label, purpose, signup in DATA_PROVIDERS:
        if ask_yes_no(f"  Add a key for {label}? ({purpose})"):
            key = ask_secret(f"     {label} API key")
            if key:
                values[env_key] = key
                print(ok(f"Saved {env_key} ({mask(key)}); manage keys at {signup}"))
            else:
                print(warn(f"Skipped {label}."))

    # ---- write ----------------------------------------------------------------
    header(3, steps, "Ready to write")
    for key, value in values.items():
        shown = mask(value) if "API_KEY" in key else value
        styled = paint(shown, Style.GREEN) if value else paint(shown, Style.DIM)
        print(f"   {paint(key, Style.BOLD)} = {styled}")
    if not values:
        print(paint("   (nothing to change; the template defaults stay)", Style.DIM))
    if not ask_yes_no(f"  Write these to {out_path.name}?", True):
        print(fail("Setup cancelled; nothing was changed."))
        return 0
    out_path.write_text(apply_values(base_text, values), encoding="utf-8")
    print(ok(f"Wrote {out_path}"))

    # ---- optional dependency install + next steps ------------------------------
    if shutil.which("uv") and ask_yes_no("  Install dependencies with `uv sync` now?", True):
        completed = subprocess.run(["uv", "sync"], cwd=REPO_ROOT)
        if completed.returncode != 0:
            print(fail("`uv sync` failed; run it manually and check the error above."))

    print()
    print(divider())
    for line in (
        "uv run app                               # start the app",
        "open http://127.0.0.1:8000               # analyze your first tickers",
        "uv run test                              # fast offline check suite",
        "README.md -> Configuration               # every knob, one table each",
        ".env                                     # your file; it is gitignored",
    ):
        print(paint("  " + line, Style.CYAN))
    print(divider())
    print()
    print(paint("  ✔ Setup complete. Happy researching!", Style.GREEN, Style.BOLD))
    print(paint("  Educational simulation, not investment advice.", Style.DIM))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n" + fail("Setup cancelled; nothing was written."))
        sys.exit(130)
