"""Raider Logbook desktop overlay (the app itself; started by overlay.py, the launcher).

An always-on-top window with the Raider Logbook UI plus a screen scanner.

Hotkeys (global, work while the game has focus):
  F8          show / hide the overlay
  F9          scan the screen (quest log or vault page, detected automatically)
  Shift+F9    start a new vault scan (then F9 on each vault page)

Run the game in borderless windowed mode so the overlay can sit on top.
The overlay only takes screenshots. It does not read game memory or files and
does not send any input to the game.

On every launch it rebuilds its data from MetaForge and the ARC Raiders Wiki in the background
(cached, see update_data) and reloads when done.

Usage:  python overlay.py [--monitor N] [--no-update]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import shutil
import sys
import threading
import time



DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "RaiderLogbook")
LAUNCH = {}                                                # filled in by the launcher

import cv2
import mss
import numpy as np
import webview
from pynput import keyboard

from scanner import Scanner
import capture                                             # noqa: E402
capture.enable_dpi_awareness()

FROZEN = hasattr(sys, "_MEIPASS")
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
ROOT = BASE if FROZEN else os.path.dirname(BASE)          # where build.py, web/ and sources/ live
sys.path.insert(0, ROOT)
BUNDLED_UI = os.path.join(BASE, "ui", "index.html") if FROZEN else os.path.join(ROOT, "dist", "index.html")
BASE_JSON = os.path.join(BASE, "assets", "base.json")
if not os.path.exists(BASE_JSON):                 # running from source with code loaded from elsewhere
    BASE_JSON = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "assets", "base.json")
SCAN_DIR = os.path.join(DATA_DIR, "scans")
PAGE_DIR = os.path.join(DATA_DIR, "page")
UI = os.path.join(PAGE_DIR, "index.html")


class Api:
    def __init__(self, monitor: int):
        self._monitor = monitor
        self._window: webview.Window | None = None
        self._scanner: Scanner | None = None
        self._lock = threading.Lock()
        self._visible = True

    def _scn(self) -> Scanner:
        if self._scanner is None:
            self._scanner = Scanner(learned_dir=os.path.join(DATA_DIR, "learned"))
        return self._scanner

    # ---- called from the UI (window.pywebview.api.*) ----
    def scan_screen(self):
        with self._lock:
            self._state("capture")
            try:
                img, info = self._grab()
                self._state("recognise")
                res = self._safe_scan(img)
            finally:
                self._state(None)
            res["capture"] = info
            return res

    def _state(self, stage):
        """Tell the page what the scanner is doing, so it can show a 'scanning…' bar."""
        try:
            self._window.evaluate_js(f"window.onScanState&&window.onScanState({json.dumps(stage)})")
        except Exception:
            pass

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

    def open_scans_folder(self):
        os.makedirs(SCAN_DIR, exist_ok=True)
        if os.name == "nt":
            os.startfile(SCAN_DIR)
        return SCAN_DIR

    def app_info(self):
        up = LAUNCH.get("update") or {}
        return {"app": LAUNCH.get("app"), "code": LAUNCH.get("code"), "scans": SCAN_DIR,
                "update": up.get("status"), "update_error": up.get("error"), "log": LAUNCH.get("log"),
                "new_app": up.get("new_app") or (up.get("download") if up.get("status") == "new_exe_needed" else None)}

    def learn(self, key, item_id):
        """'This unknown slot is item_id': remembered for future scans."""
        logging.info("correction: unknown %s -> %s", key[:16], item_id)   # so wrong matches can be reviewed
        with self._lock:
            return self._scn().learn(key, item_id)

    def reassign(self, from_id, key, to_id):
        """'These slots were recognised as from_id but are to_id': fixes this scan, remembered for later."""
        logging.info("correction: %s (%s) -> %s", from_id, key[:16], to_id)
        with self._lock:
            return self._scn().reassign(from_id, key, to_id)

    def reset_vault_session(self):
        self._scn().reset_vault_session()
        return True

    # ---- internals ----
    def _safe_scan(self, img):
        saved = self._save_scan(img)
        try:
            res = self._scn().scan(img)
        except Exception as e:  # keep the overlay alive and tell the user
            res = {"error": f"Scan failed: {e}"}
        if saved:
            res["saved"] = saved
        return res

    def _save_scan(self, img, keep=15):
        """Keep the last screenshots so a failed scan can be sent in and the scanner tuned."""
        try:
            os.makedirs(SCAN_DIR, exist_ok=True)
            path = os.path.join(SCAN_DIR, time.strftime("scan-%Y%m%d-%H%M%S.png"))
            cv2.imencode(".png", img)[1].tofile(path)
            old = sorted(f for f in os.listdir(SCAN_DIR) if f.startswith("scan-"))
            for f in old[:-keep]:
                os.remove(os.path.join(SCAN_DIR, f))
            return path
        except Exception:
            return None

    def _grab(self):
        was_visible = self._visible
        if was_visible:               # keep the overlay out of the screenshot
            self._window.hide()
            time.sleep(0.25)
        try:
            shot, info = capture.grab(self._monitor, bring_to_front=was_visible)
        finally:
            if was_visible:
                self._window.show()
        return shot, info

    def _push(self, result):
        self._window.evaluate_js(f"window.onScanResult({json.dumps(result)})")

    def _toggle(self):
        if self._visible:
            self._window.hide()
        else:
            self._window.show()
        self._visible = not self._visible


def update_data(api: Api):
    """Rebuild the page from fresh MetaForge/wiki data, then reload the window.

    Sources are cached in DATA_DIR/cache and re-downloaded when older than their limit
    (events 3 h, items and quests 12 h, map markers and wiki 24 h)."""
    try:
        log = lambda m: logging.info("[data] %s", m)
        up = LAUNCH.get("update") or {}
        import build
        page = build.run(base_path=BASE_JSON, cache=os.path.join(DATA_DIR, "cache"), out_dir=PAGE_DIR,
                         assets=False, log=log)
        # reload from inside the page: WebView2 ignores setting the address it already shows, and a
        # cache-busting ?v= on a file:// address is escaped into the file name by .NET's Uri
        # ("File not found")
        try:
            api._window.evaluate_js("location.reload()")
        except Exception:                          # the page navigates away while answering
            pass
        for _ in range(40):                        # wait until the reloaded page has its scripts (5 MB page)
            time.sleep(0.25)
            try:
                if api._window.evaluate_js("typeof window.toast === 'function'"):
                    break
            except Exception:
                pass
        msg = {"updated": f"App updated to version {up.get('version')} and data refreshed",
               "new_exe_needed": "A new version needs a fresh download from GitHub (see Help)",
               "unreachable": "Data updated (couldn't check for app updates)",
               "failed": "Data updated (app update failed, see app.log)"}.get(up.get("status"), "Data updated")
        api._window.evaluate_js(f"toast({json.dumps(msg)})")
    except Exception as e:
        logging.exception("data update failed")
        try:
            api._window.evaluate_js(f"toast({json.dumps('Could not update data: ' + str(e)[:120])})")
        except Exception:
            pass


def start_hotkeys(api: Api):
    def scan():
        def run():
            try:
                logging.info("scan: start")
                res = api.scan_screen()
                logging.info("scan: %s", {k: (v if k not in ("items", "quests", "unknowns") else len(v))
                                          for k, v in res.items() if k not in ("debug",)})
                api._push(res)
            except Exception:
                logging.exception("scan crashed")
        if api._lock.locked():
            logging.info("scan: ignored, previous scan still running")
            api._state("busy")
            return
        threading.Thread(target=run, daemon=True).start()

    def new_vault():
        api.reset_vault_session()
        api._window.evaluate_js("toast('New vault scan started. Press F9 on each vault page.')")

    hk = keyboard.GlobalHotKeys({"<f8>": api._toggle, "<f9>": scan, "<shift>+<f9>": new_vault})
    hk.daemon = True
    hk.start()


def main(launch=None):
    LAUNCH.update(launch or {})
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor", type=int, default=1, help="monitor to scan (1 = primary)")
    ap.add_argument("--width", type=int, default=560)
    ap.add_argument("--no-update", action="store_true", help="don't download fresh data on start")
    a = ap.parse_args()

    api = Api(a.monitor)
    os.makedirs(PAGE_DIR, exist_ok=True)
    if os.path.exists(BUNDLED_UI) and (not os.path.exists(UI) or os.path.getmtime(BUNDLED_UI) > os.path.getmtime(UI)):
        shutil.copyfile(BUNDLED_UI, UI)               # first run, or the app itself was updated
    if not os.path.exists(UI):                        # running from source: build the page before the window opens
        import build
        build.run(base_path=BASE_JSON, cache=os.path.join(DATA_DIR, "cache"), out_dir=PAGE_DIR, assets=False,
                  log=lambda m: logging.info("[first build] %s", m))
    with mss.mss() as sct:
        mon = sct.monitors[min(a.monitor, len(sct.monitors) - 1)]
    height = min(1000, mon["height"] - 80)
    # Open the page as file:// - the same address the data refresh reloads it with. Given a plain path,
    # pywebview serves it from http://127.0.0.1:42001 instead: a different origin with its own
    # localStorage (your vault and quests seemed gone whenever the refresh didn't reload the page), and
    # its server keeps index.html open so the refresh couldn't replace it.
    api._window = webview.create_window(
        "Raider Logbook", pathlib.Path(UI).resolve().as_uri(), js_api=api, width=a.width, height=height,
        x=mon["left"] + mon["width"] - a.width - 20, y=mon["top"] + 40,
        on_top=True, resizable=True, background_color="#12161B")
    threading.Thread(target=api._scn, daemon=True).start()   # warm up OCR models

    def on_start():
        start_hotkeys(api)
        if not a.no_update:
            threading.Thread(target=update_data, args=(api,), daemon=True).start()
    webview.start(on_start, private_mode=False, storage_path=DATA_DIR)


if __name__ == "__main__":
    main()
