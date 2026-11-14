"""Run the local server inside a desktop window."""

import threading
import time
import urllib.request
import os

import uvicorn
import webview

from app.cli import app_url, pick_port
from app.config import BASE_DIR, settings
from app.main import STATIC_DIR, app


def main() -> None:
    port = pick_port(settings.app_port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    try:
        for _ in range(200):
            if server.started:
                break
            if not worker.is_alive():
                raise RuntimeError("Local server could not start")
            time.sleep(0.1)
        else:
            raise RuntimeError("Local server did not become ready")
        with urllib.request.urlopen(f"{app_url(port)}/api/health", timeout=5) as response:
            if response.status != 200:
                raise RuntimeError("Local server health check failed")
        webview.create_window("BetterTradingAgents", app_url(port), width=1280, height=850)
        icon = "favicon.ico" if os.name == "nt" else "favicon.png"
        webview.start(icon=str(STATIC_DIR / icon), private_mode=False,
                      storage_path=str(BASE_DIR / "webview_data"))
    finally:
        server.should_exit = True
        worker.join(timeout=10)


if __name__ == "__main__":
    main()
