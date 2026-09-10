"""Offline checks for the interactive setup wizard (scripts/setup_wizard.py).

Run: PYTHONPATH=. uv run python scripts/check_setup_wizard.py
No network: the wizard runs as a subprocess with piped answers and a
temporary --out file; the optional LLM ping is declined. Windows and POSIX
both work because piped stdin makes key entry fall back to plain input().
"""

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WIZARD = REPO_ROOT / "scripts" / "setup_wizard.py"


def run_wizard(out_path: Path, answers: list[str]) -> str:
    completed = subprocess.run(
        [sys.executable, str(WIZARD), "--out", str(out_path)],
        input="\n".join(answers) + "\n",
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def env_dict(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


with tempfile.TemporaryDirectory() as tmp:
    # ---- preset path: Z.AI, key entered, ping declined, write, no uv sync ----
    env_file = Path(tmp) / "one.env"
    stdout = run_wizard(env_file, [
        "2",               # provider: Z.AI (GLM)
        "",                # base URL: accept preset
        "",                # model: accept preset
        "test-key-123",    # API key (plain input: stdin is piped)
        "n",               # do not test the key (no network in checks)
        "n", "n", "n",     # skip Finnhub / Olostep / Nixtla
        "y",               # write the file
        "n",               # do not run uv sync
    ])
    values = env_dict(env_file)
    assert values["LLM_BASE_URL"] == "https://api.z.ai/api/paas/v4", values
    assert values["LLM_MODEL"] == "glm-5.3-flash", values
    assert values["LLM_API_KEY"] == "test-key-123", values
    # The template's full knob list ships with the file, not just the answers.
    for knob in ("LLM_PRICE_IN", "STREAM_REASONING", "CALIBRATION_MIN_OBSERVATIONS",
                 "MAX_POSITION_PCT", "MEMORY_HORIZON_DAYS", "DEBATE_ROUNDS"):
        assert knob in values, f"{knob} missing from written .env"
    assert values["FINNHUB_API_KEY"] == ""
    # The key must never appear verbatim in the wizard's own output.
    assert "test-key-123" not in stdout, "API key leaked into wizard output"
    print("preset path OK: Z.AI values written, template complete, key masked")

    # ---- mock path: provider skipped, no key at all ---------------------------
    env_file = Path(tmp) / "two.env"
    run_wizard(env_file, [
        "7",               # skip: mock mode
        "n", "n", "n",     # skip data providers
        "y",               # write
        "n",               # no uv sync
    ])
    values = env_dict(env_file)
    assert values["LLM_API_KEY"] == "" and values["LLM_MODEL"] == "gpt-5.6-luna"
    print("mock path OK: no key required, defaults intact")

    # ---- merge path: existing .env values survive a re-run ---------------------
    env_file = Path(tmp) / "three.env"
    env_file.write_text("MAX_TICKERS=3\nLLM_API_KEY=keep-me\n", encoding="utf-8")
    run_wizard(env_file, [
        "",                # keep existing values, only fill what is missing
        "7",               # mock mode: do not touch LLM_* values
        "n", "n", "n",     # skip data providers
        "y",               # write
        "n",               # no uv sync
    ])
    values = env_dict(env_file)
    assert values["MAX_TICKERS"] == "3" and values["LLM_API_KEY"] == "keep-me", values
    print("merge path OK: existing values preserved")

print("ALL SETUP WIZARD CHECKS PASSED")
