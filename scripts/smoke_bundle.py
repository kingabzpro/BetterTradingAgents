"""Launch an extracted release archive outside the source checkout."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def smoke(archive: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="bta bundle ") as temp:
        root = Path(temp)
        shutil.unpack_archive(archive, root)
        bundle = root / "bettertradingagents"
        exe = bundle / ("bettertradingagents.exe" if os.name == "nt" else "bettertradingagents")
        assert exe.is_file(), f"Missing executable in {archive.name}"
        assert (bundle / "_internal/agents/sandbox/memory/prompts/memory_consolidation_prompt.md").is_file(), "OpenAI Agents prompt is missing"
        assert not (bundle / "portfolio.db").exists(), "Archive contains a portfolio database"
        assert not (bundle / ".env").exists(), "Archive contains a .env file"

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        (bundle / ".env").write_text(f"APP_PORT={port}\nMAX_TICKERS=4\n", encoding="utf-8")
        env = {
            key: os.environ[key]
            for key in ("SystemRoot", "TEMP", "TMP", "TMPDIR")
            if key in os.environ
        }
        env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
        client = build_opener(ProxyHandler({}))
        base = f"http://127.0.0.1:{port}"
        with (root / "server.log").open("w+b") as log:
            process = subprocess.Popen([str(exe)], cwd=root, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        with client.open(f"{base}/api/health", timeout=1) as response:
                            health = json.load(response)
                        break
                    except (URLError, TimeoutError):
                        time.sleep(0.25)
                else:
                    log.seek(0)
                    raise AssertionError(f"Packaged app failed to start:\n{log.read().decode(errors='replace')}")

                assert health["mock_mode"] and health["max_tickers"] == 4, health
                with client.open(f"{base}/", timeout=5) as response:
                    assert b"BetterTradingAgents" in response.read()
                with client.open(f"{base}/static/js/app.js", timeout=5) as response:
                    assert b"import " in response.read()
                with client.open(f"{base}/api/settings", timeout=5) as response:
                    assert json.load(response)["groups"]
                assert (bundle / "portfolio.db").is_file(), "Database should be beside the executable"
                assert not (root / "portfolio.db").exists(), "Database escaped the portable app folder"
            finally:
                process.terminate()
                process.wait(timeout=10)
    print(f"Extracted app smoke passed: {archive.name}")


if __name__ == "__main__":
    smoke(Path(sys.argv[1]).resolve())
