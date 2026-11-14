"""Launch an extracted release archive outside the source checkout."""

import json
import ctypes
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
            for key in ("SystemRoot", "TEMP", "TMP", "TMPDIR", "HOME", "DISPLAY", "XAUTHORITY", "PATH",
                        "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "XDG_RUNTIME_DIR")
            if key in os.environ
        }
        env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
        if os.name != "nt" and "-desktop-" in archive.name:
            env["QT_DEBUG_PLUGINS"] = "1"
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
                if "-desktop-" in archive.name:
                    time.sleep(5)
                    if process.poll() is not None:
                        log.seek(0)
                        raise AssertionError(f"Desktop window exited during startup:\n{log.read().decode(errors='replace')}")
                with client.open(f"{base}/", timeout=5) as response:
                    assert b"BetterTradingAgents" in response.read()
                with client.open(f"{base}/static/js/app.js", timeout=5) as response:
                    assert b"import " in response.read()
                with client.open(f"{base}/api/settings", timeout=5) as response:
                    assert json.load(response)["groups"]
                assert (bundle / "portfolio.db").is_file(), "Database should be beside the executable"
                assert not (root / "portfolio.db").exists(), "Database escaped the portable app folder"
            finally:
                if os.name == "nt" and "-desktop-" in archive.name and process.poll() is None:
                    user32 = ctypes.windll.user32
                    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

                    @callback_type
                    def close_window(handle, _):
                        pid = ctypes.c_ulong()
                        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                        title = ctypes.create_unicode_buffer(128)
                        user32.GetWindowTextW(handle, title, len(title))
                        if pid.value == process.pid and title.value == "BetterTradingAgents":
                            user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE
                        return True

                    user32.EnumWindows(close_window, 0)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                        raise AssertionError("Desktop did not exit when its window closed")
                    time.sleep(3)  # WebView2 releases its profile after the app exits.
                elif process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
    print(f"Extracted app smoke passed: {archive.name}")


if __name__ == "__main__":
    smoke(Path(sys.argv[1]).resolve())
