"""Console entry points for the three everyday commands (pyproject.toml).

    uv run setup              interactive first-run wizard
    uv run app                start the FastAPI app with autoreload
    uv run test               offline check suite; add names for specific
                              checks (uv run test chat) or "all" for every
                              scripts/check_*.py
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fast, fully offline checks that exercise the core: indicators and
# accounting, the cost estimate, durable history, and the setup wizard.
FAST_CHECKS = ("quick_wins", "cost", "run_history", "setup_wizard")


def serve() -> int:
    """Entry point for `uv run app`: start the server with autoreload."""
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
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
    else:
        names = requested or list(FAST_CHECKS)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
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
