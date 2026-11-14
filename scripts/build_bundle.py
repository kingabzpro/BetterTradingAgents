"""Freeze the app and archive the executable with its bundled runtime."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "bettertradingagents"


def build(output: Path, desktop: bool = False) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    platform, archive_format = ("windows", "zip") if os.name == "nt" else ("linux", "gztar")
    with tempfile.TemporaryDirectory(prefix="bta-build-") as temp:
        work = Path(temp)
        dist = work / "dist"
        options = (
            ["--windowed", "--icon", str(ROOT / "static" / "favicon.ico"), "--collect-all", "webview"]
            if desktop else []
        )
        subprocess.run(
            [
                sys.executable, "-m", "PyInstaller", "--noconfirm", "--noupx",
                "--name", NAME,
                "--distpath", str(dist),
                "--workpath", str(work / "build"),
                "--specpath", temp,
                "--paths", str(ROOT),
                "--add-data", f"{ROOT / 'static'}:static",
                "--collect-data", "agents",
                *options,
                str(ROOT / "scripts" / ("desktop.py" if desktop else "bundle_entry.py")),
            ],
            cwd=ROOT,
            check=True,
        )
        for filename in ("LICENSE", ".env.example"):
            shutil.copy2(ROOT / filename, dist / NAME)
        archive = shutil.make_archive(
            str(output / f"{NAME}-{'desktop-' if desktop else ''}{platform}-x64"), archive_format,
            root_dir=dist, base_dir=NAME,
        )
    return Path(archive)


if __name__ == "__main__":
    print(build(Path(sys.argv[1]).resolve(), desktop="--desktop" in sys.argv[2:]))
