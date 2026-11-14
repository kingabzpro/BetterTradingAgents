"""The desktop window starts a healthy server and shuts it down on close."""

import json
import os
import sys
import tempfile
import types
from urllib.request import urlopen

with tempfile.TemporaryDirectory() as temp:
    os.environ.update(DB_PATH=f"{temp}/portfolio.db", LLM_API_KEY="", APP_PORT="19387")
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    window = types.ModuleType("webview")
    address = []
    window.create_window = lambda title, url, **kwargs: address.append(url)

    def close_window(**kwargs):
        with urlopen(address[0] + "/api/health", timeout=5) as response:
            assert json.load(response)["mock_mode"]

    window.start = close_window
    sys.modules["webview"] = window
    from scripts.desktop import main

    main()
    try:
        urlopen(address[0] + "/api/health", timeout=2)
    except OSError:
        print("PASS desktop server starts and stops with its window")
    else:
        raise AssertionError("desktop server still runs after the window closed")
