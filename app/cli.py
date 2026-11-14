"""Console entry points for the three everyday commands (pyproject.toml).

    uv run setup              interactive first-run wizard
    uv run app                start the FastAPI app
    uv run test               offline check suite; add names for specific
                              checks (uv run test chat), "all" for every
                              scripts/check_*.py, or "offline" for everything
                              except the browser smoke
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fast, fully offline checks that exercise the core: indicators and
# accounting, the cost estimate, durable history, the setup wizard, and
# the P2.23 security guarantees (rebinding, headers, rate limits, secrets).
FAST_CHECKS = ("quick_wins", "cost", "run_history", "changes", "ui", "accuracy", "watchlist", "search_export", "paper_trading", "setup_wizard", "security")

APP_URL = "http://127.0.0.1:8000"
WIKI_URL = "https://github.com/kingabzpro/BetterTradingAgents/wiki"


def app_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int, attempts: int = 20) -> int:
    """The preferred port when free, else the next free one; the preferred
    port itself when everything in the range is taken, so uvicorn surfaces
    the bind failure instead of the app failing silently."""
    for candidate in range(preferred, min(preferred + attempts, 65536)):
        if _port_free(candidate):
            return candidate
    return preferred


def startup_banner(port: int = 8000) -> str:
    """Welcome block printed by `uv run app`. With the terminal quiet by
    default (TERMINAL_LOGGING=0) this is the one thing a user sees at
    startup, so it carries the actual link (the port the app really got),
    the docs, and the log hint."""
    color = (
        not os.environ.get("NO_COLOR")
        and (os.environ.get("BTA_WIZARD_COLOR") == "1" or sys.stdout.isatty())
    )
    if color:
        green, bold, dim, label, link, rule = (
            "\033[32m", "\033[1m", "\033[2m", "\033[35m", "\033[96m\033[4m", "\033[35m"
        )
        reset = "\033[0m"
    else:
        green = bold = dim = label = link = rule = reset = ""
    frozen = getattr(sys, "frozen", False)
    rule_bar = ("-" if frozen else "─") * 68
    commands = (
        f"{label}  Setup {reset}  {dim}Open Settings in the web app{reset}\n"
        if frozen
        else (
            f"{label}  Setup {reset}  {dim}uv run setup{reset}\n"
            f"{label}     Tests {reset}  {dim}uv run test{reset}\n"
        )
    )
    return (
        f"\n{rule}  {rule_bar}{reset}\n"
        f"{green}{bold}  BetterTradingAgents{reset}"
        f"{dim} - multi-agent stock research and paper trading{reset}\n"
        f"{rule}  {rule_bar}{reset}\n\n"
        f"{label}  App   {reset}  {link}{app_url(port)}{reset}\n"
        f"{label}  Docs  {reset}  {link}{WIKI_URL}{reset}\n"
        f"{commands}\n"
        f"{dim}  Ctrl+C stops the server.{reset}\n"
        f"{dim}  Set TERMINAL_LOGGING=1 in .env for the full log trail.{reset}\n"
    )


def serve() -> int:
    """Entry point for `uv run app`: start the server on APP_PORT (default
    8000), automatically bumping to the next free port when it is taken.

    No auto-reload: uvicorn's reload worker spawns the base interpreter and
    has served stale code silently after code changes (twice). A plain
    single process always runs exactly what is on disk; restart with
    Ctrl+C and `uv run app` to pick up changes.
    """
    import uvicorn

    from app.config import settings
    from app.main import app

    port = pick_port(settings.app_port)
    print(startup_banner(port), flush=True)
    if port != settings.app_port:
        print(
            f"  \033[33mPort {settings.app_port} is busy; serving on {port} instead.\033[0m"
            if not os.environ.get("NO_COLOR")
            else f"  Port {settings.app_port} is busy; serving on {port} instead."
        )
    # Terminal stays quiet by default (TERMINAL_LOGGING=1 in .env for the
    # INFO trail and per-request access lines).
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info" if settings.terminal_logging else "warning",
        access_log=settings.terminal_logging,
    )
    return 0


def _check_path(name: str) -> Path:
    stem = name[6:] if name.startswith("check_") else name
    return REPO_ROOT / "scripts" / f"check_{stem}.py"


def test() -> int:
    """Entry point for `uv run test [name ... | all]`.

    Each check runs in its own subprocess: several of them set DB_PATH and
    other environment variables at import time, so they must never share a
    process. PYTHONPATH is set for check scripts that import `app` before
    the editable install is on sys.path.
    """
    requested = sys.argv[1:]
    if requested in (["all"], ["--all"], ["-a"]):
        names = sorted(path.stem for path in (REPO_ROOT / "scripts").glob("check_*.py"))
    elif requested in (["offline"], ["--offline"]):
        # Everything except the browser smoke: that one needs a system
        # Chromium and flakes often enough that a red/green gate would lie.
        # This is the selection CI runs (ROADMAP P2.15).
        names = sorted(
            path.stem
            for path in (REPO_ROOT / "scripts").glob("check_*.py")
            if path.stem != "check_browser_smoke"
        )
    else:
        names = requested or list(FAST_CHECKS)
    # Developer .env values must never override a check's scratch DB or keys.
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHON_DOTENV_DISABLED": "1"}
    failed: list[str] = []
    for name in names:
        script = _check_path(name)
        if not script.exists():
            print(f"unknown check: {name} (no {script.relative_to(REPO_ROOT)})")
            failed.append(name)
            continue
        print(f"\n==== {script.stem} ====")
        if subprocess.call([sys.executable, str(script)], env=env, cwd=REPO_ROOT) != 0:
            failed.append(script.stem)
    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1
    print(f"\n{len(names)} check group(s) passed")
    return 0
