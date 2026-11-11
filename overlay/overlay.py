"""Raider Logbook desktop overlay.

An always-on-top window with the Raider Logbook UI plus a screen scanner.

Hotkeys (global, work while the game has focus):
  F8          show / hide the overlay
  F9          scan the screen (quest log or vault page, detected automatically)
  Shift+F9    start a new vault scan (then F9 on each vault page)

Run the game in borderless windowed mode so the overlay can sit on top.
The overlay only takes screenshots. It does not read game memory or files and
does not send any input to the game.

Usage:  python overlay.py [--monitor N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import cv2
import mss
import numpy as np
import webview
from pynput import keyboard

from scanner import Scanner

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(BASE, "ui", "index.html")
if not os.path.exists(UI):  # running from the source tree
    UI = os.path.join(os.path.dirname(BASE), "dist", "index.html")
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "RaiderLogbook")


class Api:
    def __init__(self, monitor: int):
        self._monitor = monitor
        self._window: webview.Window | None = None
        self._scanner: Scanner | None = None
        self._lock = threading.Lock()
        self._visible = True

    def _scn(self) -> Scanner:
        if self._scanner is None:
            self._scanner = Scanner()
        return self._scanner

    # ---- called from the UI (window.pywebview.api.*) ----
    def scan_screen(self):
        with self._lock:
            img = self._grab()
            return self._safe_scan(img)

    def scan_file(self):
        paths = self._window.create_file_dialog(
            webview.OPEN_DIALOG, file_types=("Images (*.png;*.jpg;*.jpeg;*.bmp;*.webp)", "All files (*.*)"))
        if not paths:
            return None
        img = cv2.imdecode(np.fromfile(paths[0], dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return {"error": "That file isn't an image the scanner can read."}
        with self._lock:
            return self._safe_scan(img)

    def reset_vault_session(self):
        self._scn().reset_vault_session()
        return True

    # ---- internals ----
    def _safe_scan(self, img):
        try:
            return self._scn().scan(img)
        except Exception as e:  # keep the overlay alive and tell the user
            return {"error": f"Scan failed: {e}"}

    def _grab(self):
        was_visible = self._visible
        if was_visible:               # keep the overlay out of the screenshot
            self._window.hide()
            time.sleep(0.25)
        try:
            with mss.mss() as sct:
                mon = sct.monitors[min(self._monitor, len(sct.monitors) - 1)]
                shot = np.array(sct.grab(mon))[:, :, :3]
        finally:
            if was_visible:
                self._window.show()
        return np.ascontiguousarray(shot)

    def _push(self, result):
        self._window.evaluate_js(f"window.onScanResult({json.dumps(result)})")

    def _toggle(self):
        if self._visible:
            self._window.hide()
        else:
            self._window.show()
        self._visible = not self._visible


def start_hotkeys(api: Api):
    def scan():
        threading.Thread(target=lambda: api._push(api.scan_screen()), daemon=True).start()

    def new_vault():
        api.reset_vault_session()
        api._window.evaluate_js("toast('New vault scan started. Press F9 on each vault page.')")

    hk = keyboard.GlobalHotKeys({"<f8>": api._toggle, "<f9>": scan, "<shift>+<f9>": new_vault})
    hk.daemon = True
    hk.start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor", type=int, default=1, help="monitor to scan (1 = primary)")
    ap.add_argument("--width", type=int, default=560)
    a = ap.parse_args()

    api = Api(a.monitor)
    with mss.mss() as sct:
        mon = sct.monitors[min(a.monitor, len(sct.monitors) - 1)]
    height = min(1000, mon["height"] - 80)
    api._window = webview.create_window(
        "Raider Logbook", UI, js_api=api, width=a.width, height=height,
        x=mon["left"] + mon["width"] - a.width - 20, y=mon["top"] + 40,
        on_top=True, resizable=True, background_color="#12161B")
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=api._scn, daemon=True).start()   # warm up OCR models
    webview.start(lambda: start_hotkeys(api), private_mode=False, storage_path=DATA_DIR)


if __name__ == "__main__":
    main()
