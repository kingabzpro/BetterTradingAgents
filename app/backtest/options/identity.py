"""Identify the options engine loaded by this server process."""

from hashlib import file_digest, sha256
from pathlib import Path
import subprocess
import sys


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              check=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def engine_identity(root: Path) -> dict:
    if getattr(sys, "frozen", False):
        with Path(sys.executable).open("rb") as executable:
            return {"kind": "bundled_executable", "build_sha256": file_digest(executable, "sha256").hexdigest()}

    commit = _git(root, "rev-parse", "HEAD")
    dirty_scope = _git(root, "status", "--porcelain", "--untracked-files=all", "--", "app/backtest/options") if commit else None
    source = sha256()
    for path in sorted((root / "app" / "backtest" / "options").glob("*.py")):
        # Canonical newlines keep the same source identity on Windows and CI.
        content = path.read_bytes().replace(b"\r\n", b"\n")
        source.update(path.name.encode() + b"\0" + sha256(content).digest())
    fingerprint = source.hexdigest()
    return {"kind": "source", "source_commit": commit, "source_sha256": fingerprint,
            "dirty": bool(dirty_scope) if dirty_scope is not None else None,
            "dirty_source_sha256": fingerprint if dirty_scope else None}
