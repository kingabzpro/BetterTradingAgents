"""Interactive setup wizard for new users (run this first).

    uv run setup                                 (preferred; also installs deps)
    python scripts/setup_wizard.py               (stdlib only, no deps needed)

Asks a handful of questions, optionally tests the LLM key against the
provider, and writes .env from .env.example. Nothing is written until the
final confirmation, so Ctrl+C at any prompt is safe. Existing values in
.env are kept by default. Uses only the standard library so it also runs
before `uv sync`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
TEMPLATE_PATH = REPO_ROOT / ".env.example"

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

    print()
    print("BetterTradingAgents setup")
    print("=" * 60)
    print("A few questions and you are running. Everything is optional;")
    print("anything you skip falls back to a documented default, and with no")
    print("LLM key the app runs in clearly labeled mock mode on live market")
    print("data. Ctrl+C cancels safely at any prompt.")

    if sys.version_info < (3, 12):
        print(f"\nWARNING: Python {sys.version.split()[0]} found; the app needs >= 3.12.")

    # ---- base text: keep an existing .env, else start from the template ------
    base_text = None
    if out_path.exists():
        print(f"\nFound an existing {out_path.name}.")
        if ask_yes_no("Keep its current values and only fill what is missing?", True):
            base_text = out_path.read_text(encoding="utf-8")
        elif not ask_yes_no("Overwrite it from the template?", False):
            print("Setup cancelled; nothing was changed.")
            return 0
    if base_text is None:
        try:
            base_text = TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError:
            print(f"ERROR: {TEMPLATE_PATH} is missing; cannot build {out_path.name}.")
            return 1

    # ---- LLM provider ---------------------------------------------------------
    print("\n1) LLM provider (any OpenAI-compatible endpoint)")
    for number, provider in enumerate(PROVIDERS, start=1):
        print(f"   {number}) {provider['label']}")
    choice = ask("Choose", "2")  # the README's recommended starter (GLM-5.3-Flash)
    try:
        provider = PROVIDERS[int(choice) - 1]
    except (ValueError, IndexError):
        provider = PROVIDERS[0]

    values: dict[str, str] = {}
    if provider["key"] == "mock":
        print("   Mock mode it is. You can add a key later in .env.")
    else:
        base_url = ask("   Base URL", provider["base_url"])
        model = ask("   Model", provider["model"])
        api_key = ask_secret("   API key (input hidden; Enter to skip and edit .env later)")
        values.update(
            {
                "LLM_BASE_URL": base_url,
                "LLM_MODEL": model,
                "LLM_API_KEY": api_key,
            }
        )
        if api_key and ask_yes_no("   Test the key with a one-token request now?", True):
            ok, detail = ping_llm(base_url, api_key, model)
            mark = "OK" if ok else "WARNING"
            print(f"   [{mark}] {detail}")
            if not ok:
                print("   The wizard will still save your answers; fix and retry later.")
        elif not api_key:
            print("   No key entered: the app will start in mock mode until you add one.")

    # ---- data providers -------------------------------------------------------
    print("\n2) Market-data providers (all optional; each has a built-in fallback)")
    for env_key, label, purpose, signup in DATA_PROVIDERS:
        if ask_yes_no(f"   Add a key for {label}? ({purpose})"):
            key = ask_secret(f"      {label} API key")
            if key:
                values[env_key] = key
                print(f"      Saved {env_key} ({mask(key)}); sign up or manage keys at {signup}")
            else:
                print(f"      Skipped {label}.")

    # ---- write ----------------------------------------------------------------
    print("\n3) Ready to write")
    for key, value in values.items():
        shown = mask(value) if "API_KEY" in key else value
        print(f"   {key}={shown}")
    if not ask_yes_no(f"   Write these to {out_path.name}?", True):
        print("Setup cancelled; nothing was changed.")
        return 0
    out_path.write_text(apply_values(base_text, values), encoding="utf-8")
    print(f"   Wrote {out_path}.")

    # ---- optional dependency install + next steps ------------------------------
    if shutil.which("uv") and ask_yes_no("4) Install dependencies with `uv sync` now?", True):
        completed = subprocess.run(["uv", "sync"], cwd=REPO_ROOT)
        if completed.returncode != 0:
            print("   `uv sync` failed; run it manually and check the error above.")

    print()
    print("Next steps")
    print("  uv run app                               # start the app")
    print("  open http://127.0.0.1:8000               # analyze your first tickers")
    print("  uv run test                              # fast offline check suite")
    print("  README.md -> Configuration               # every knob, one table each")
    print("  .env                                     # your file; it is gitignored")
    print()
    print("Setup complete. Happy researching - this is an educational")
    print("simulation, not investment advice.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nSetup cancelled; nothing was written.")
        sys.exit(130)
