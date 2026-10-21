"""
Desktop launcher for ESP 32 RGB Controller.

Double-clicking this file:

1. Starts the controller (app_v2.py) in the background, with no console window.
   It is told to switch the strip ON in the mode you used last.
2. Waits until the controller is actually pushing frames.
3. Opens the dashboard in your default browser, on the page you last used.

Run it again while the controller is already running and it simply opens the
browser - it never starts a second copy.

Pass --no-browser when it is launched at Windows startup: the lights come on
but no browser window is opened.

Logs land in controller.log (application log) and controller_console.log
(everything the process printed), both next to this file.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT = 5000
BASE_URL = f"http://localhost:{PORT}"

# Windows process flags: leave no console window behind
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def server_status(timeout: float = 1.5):
    """Return the controller's ready state, or None when it is not running."""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/ready", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def start_controller():
    """Launch app_v2.py detached, with autostart and file logging enabled."""
    env = dict(os.environ)
    env["LED_AUTOSTART"] = "1"
    env["LED_LOG"] = str(BASE / "controller.log")

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

    console = open(BASE / "controller_console.log", "ab")
    try:
        subprocess.Popen(
            [sys.executable, str(BASE / "app_v2.py")],
            cwd=str(BASE),
            env=env,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=console,
            stderr=console,
            close_fds=True,
        )
    finally:
        console.close()


def last_page() -> str:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/last-page", timeout=2) as r:
            return json.load(r).get("page", "home")
    except Exception:
        return "home"


def open_dashboard():
    page = last_page()
    url = BASE_URL if page in ("", "home", None) else f"{BASE_URL}/{page}"
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    status = server_status()

    if status is None:
        start_controller()

        # Wait up to 30 s for the controller to answer and push its first frame
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.4)
            status = server_status()
            if status and status.get("ready"):
                break

    # Started by Windows at login: light the strip, but do not steal focus
    # with a browser window.
    if "--no-browser" not in sys.argv:
        open_dashboard()


if __name__ == "__main__":
    main()
