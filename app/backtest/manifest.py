"""Reproducibility manifest for backtest reports (ROADMAP P1.2).

A manifest is written beside every report: code revision, model, decision
policy version, ticker universe, dates, costs, a hash of every snapshot the
run consumed, and the random seeds. With the manifest and the snapshot cache,
the same mock report must reproduce; the check script enforces that.
"""

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.grade import BOOTSTRAP_REPS

SCHEMA_VERSION = 1


def _git(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def code_revision() -> dict:
    revision = _git(["rev-parse", "--short", "HEAD"])
    dirty = bool(_git(["status", "--porcelain"]))
    return {"revision": revision or "unknown", "dirty": dirty}


def payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_manifest(
    config: dict, snapshot_payloads: dict[tuple[str, str], dict | None]
) -> dict:
    """Manifest content for one run.

    `snapshot_payloads` maps (ticker, date) to the cached payload the run
    consumed (None for failed snapshots); each is hashed so a report can be
    reproduced only from the same data.
    """
    revision = code_revision()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code": revision,
        "decision_policy_version": config.get("policy_version", ""),
        "config": {
            key: value
            for key, value in config.items()
            if key not in ("report_json", "report_md", "manifest_path")
        },
        "data": {
            "snapshot_hashes": {
                f"{ticker}@{day}": (
                    "missing" if payload is None else payload_hash(payload)
                )
                for (ticker, day), payload in sorted(snapshot_payloads.items())
            },
        },
        "random_seeds": {
            "bootstrap": config.get("seed", 7),
            "bootstrap_reps": BOOTSTRAP_REPS,
            "pipeline": "deterministic (mock mode)"
            if config.get("mode") == "mock"
            else "LLM sampling: not reproducible run-to-run",
        },
    }


def write_manifest(manifest: dict, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"manifest-{name}.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path
