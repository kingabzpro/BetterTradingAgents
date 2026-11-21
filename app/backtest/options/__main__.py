"""Validate, install, and inspect local options backtest data packages."""

import argparse
from datetime import date
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.backtest.options.data import MAX_DATA_BYTES, ROOT, load_dataset
from app.backtest.options.domain import Dataset, PRESETS
from app.backtest.options.jobs import Jobs, Request, encoded


def install(path: Path, jobs: Jobs, preset: str, replace: bool) -> Path:
    rules = PRESETS[preset]
    target = jobs.root / rules.mode / preset / "data" / "historical.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as source:
        raw = source.read(MAX_DATA_BYTES + 1)
    if len(raw) > MAX_DATA_BYTES:
        raise ValueError("import exceeds 128 MiB; use targeted date windows")
    with NamedTemporaryFile(dir=target.parent, suffix=".json", delete=False) as scratch:
        scratch.write(raw)
        staged = Path(scratch.name)
    try:
        load_dataset(staged, rules)
        if replace:
            staged.replace(target)
        else:
            # A hard link publishes the validated bytes atomically and refuses to replace an existing input.
            target.hardlink_to(staged)
    finally:
        staged.unlink(missing_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="print the normalized JSON data schema")
    importer = commands.add_parser("import", help="validate and install an entitled historical JSON package")
    importer.add_argument("preset", choices=PRESETS)
    importer.add_argument("path", type=Path)
    importer.add_argument("--replace", action="store_true", help="explicitly replace the previous import for this preset")
    checker = commands.add_parser("plan", help="show archived dates, calendar targets and readiness")
    checker.add_argument("preset", choices=PRESETS)
    checker.add_argument("--start", type=date.fromisoformat)
    checker.add_argument("--end", type=date.fromisoformat)
    checker.add_argument("--source", choices=("historical", "synthetic"), default="historical")
    args = parser.parse_args()
    jobs = Jobs(ROOT / "local-ops" / "backtests" / "options")
    try:
        if args.command == "schema":
            print(json.dumps(Dataset.model_json_schema(), indent=2))
        elif args.command == "import":
            print(install(args.path, jobs, args.preset, args.replace))
        else:
            dates = jobs.metadata()["signal_dates"]
            request = Request(mode=PRESETS[args.preset].mode, preset=args.preset, source=args.source,
                              start=args.start or dates[0], end=args.end or dates[-1])
            print(json.dumps(encoded(jobs.prepare(request)[-1]), indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
