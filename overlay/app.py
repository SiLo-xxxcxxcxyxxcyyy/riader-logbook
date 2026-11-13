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
import marks                                               # noqa: E402
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
        self._map_window: webview.Window | None = None
        self._mon = None                                    # monitor rectangle, set in main()
        self._map_url = None
        self._excluded = set()                              # our windows Windows keeps out of screenshots
        self._values = {}                                   # item id -> value in creds (sent by the page)
        self._marks = None                                  # in-game marks layer (marks.Marks)
        self._watcher: Watcher | None = None

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

    def open_map(self, url):
        """Show a map page (ARC Tracker's item spawn map) in a second always-on-top window, next to the
        overlay. It's an ordinary visit of their page, like opening it in a browser. One window, reused."""
        if not isinstance(url, str) or not url.startswith("https://arctracker.io/maps/"):
            return False
        w = self._map_window
        if w is not None:
            try:
                if url != self._map_url:
                    w.load_url(url)
                w.show()
                w.restore()
                self._map_url = url
                return True
            except Exception:
                self._map_window = None
        mon = self._mon or {"left": 0, "top": 0, "width": 1920, "height": 1080}
        width = min(1000, mon["width"] - 640)
        height = min(900, mon["height"] - 120)
        try:                                             # just left of the overlay
            x = max(mon["left"], self._window.x - width - 10)
        except Exception:
            x = mon["left"] + 40
        self._map_window = webview.create_window(
            "Item map · ARC Tracker", url, width=width, height=height, x=x, y=mon["top"] + 40,
            on_top=True, resizable=True, background_color="#12161B")
        self._map_url = url

        def closed():
            self._map_window = None
            self._map_url = None
        self._map_window.events.closed += closed
        self._map_window.events.shown += lambda: self._exclude(self._map_window, "map")
        return True

    def reset_vault_session(self):
        self._scn().reset_vault_session()
        return True

    def set_auto(self, on):
        """Turn the automatic reading (background watcher) on or off."""
        if self._watcher:
            self._watcher.on = bool(on)
            logging.info("auto read: %s", "on" if on else "off")
        return bool(self._watcher and self._watcher.on)

    def set_item_values(self, values):
        """The page sends each item's value, shown under the slots in the game."""
        if isinstance(values, dict):
            self._values = {str(k): float(v) for k, v in values.items() if isinstance(v, (int, float))}
        return len(self._values)

    def set_marks(self, on):
        if self._marks:
            self._marks.enabled = bool(on)
            if not on:
                self._marks.clear()
        return bool(self._marks and self._marks.enabled)

    def js_error(self, msg):
        """An error in the page (logged so a problem can be found later)."""
        logging.warning("page error: %s", str(msg)[:2000])
        return True

    def auto_status(self):
        w = self._watcher
        return {"on": bool(w and w.on), "state": w.state if w else "off", "excluded": sorted(self._excluded)}

    def _exclude(self, window, name):
        """Keep a window out of screen capture, so it can stay visible while the game is read."""
        try:
            hwnd = window.native.Handle.ToInt64()
        except Exception:
            logging.exception("no window handle for %s", name)
            return False
        ok = capture.exclude_from_capture(hwnd)
        if ok:
            self._excluded.add(name)
        logging.info("exclude %s from capture: %s", name, ok)
        return ok

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
        if self._watcher:
            self._watcher.stream.stop()                     # dxcam: one capture at a time per screen
        if "main" in self._excluded and (self._map_window is None or "map" in self._excluded):
            return capture.grab(self._monitor, bring_to_front=True)   # the overlay isn't in the picture
        was_visible = self._visible
        mw = self._map_window
        if was_visible:               # keep the overlay (and the map window) out of the screenshot
            self._window.hide()
            if mw is not None:
                self._safe(mw.hide)
            time.sleep(0.25)
        try:
            shot, info = capture.grab(self._monitor, bring_to_front=was_visible)
        finally:
            if was_visible:
                self._window.show()
                if mw is not None:
                    self._safe(mw.show)
        return shot, info

    def _push(self, result):
        self._window.evaluate_js(f"window.onScanResult({json.dumps(result)})")

    @staticmethod
    def _safe(fn):
        try:
            fn()
        except Exception:
            pass

    def _toggle(self):
        mw = self._map_window
        if self._visible:
            self._window.hide()
            if mw is not None:
                self._safe(mw.hide)
        else:
            self._window.show()
            if mw is not None:
                self._safe(mw.show)
        self._visible = not self._visible


class Watcher(threading.Thread):
    """Reads the game by itself: a few frames a second of the game window (only while the game is in
    front), and when the stash or an open quest has settled on screen and changed since the last read,
    it is scanned and the result sent to the page (window.onAutoScan). Cheap screens only: the stash
    (rows already read come from a cache) and a quest at a trader (fixed places, ~0.5 s). Anything
    else is ignored without screen-wide text recognition."""

    ROIS = {"stash": (100, 250, 720, 1080), "quest": (840, 100, 1880, 980)}   # 1080p: stash panel, quest panel
    NEW_VISIT = 30.0          # seconds without the stash after which it is read from scratch again

    def __init__(self, api: Api):
        super().__init__(daemon=True)
        self.api, self.on, self.state = api, True, "starting"
        self.stream = capture.Stream(fps=4)
        self.prev, self.since, self.done = {}, {}, {}
        self.last_stash, self.last_scan = 0.0, 0.0
        self._last_slots = []

    def _status(self, f, text, colour):
        m = self.api._marks
        if m:
            k = f.shape[0] / 1080
            ox, oy = self.stream.origin
            m.show((int(ox), int(oy), f.shape[1], f.shape[0]), [], (text, colour, (int(150 * k), int(122 * k))))
            self.marked = True

    def _show_marks(self, f, frame, res=None, busy=None):
        m = self.api._marks
        if not m or not frame:
            return
        vals = self.api._values
        slots = []
        for x, y, w, h, iid, n, *state in frame:
            v = vals.get(iid, 0) * n if iid else 0
            slots.append({"box": (x, y, w, h), "ok": iid is not None, "busy": bool(state and state[0] == "busy"),
                          "label": marks.short_value(v) if v else None})
        self._last_slots = slots
        status = None
        st = (res or {}).get("stash") or {}
        if res is not None:
            k = f.shape[0] / 1080
            if res.get("gap"):
                status = ("Rows were skipped: scroll back up a bit", "warn")
            elif st.get("used") and st.get("slots", 0) >= st["used"]:
                status = (f"All {st['used']} read" if st.get("slots") == st["used"] else
                          f"Read {st['slots']} of {st['used']}: check it in the app", "ok" if st.get("slots") == st["used"] else "warn")
            elif st.get("used"):
                status = (f"{st['slots']} / {st['used']} read · scroll on", "busy")
            if status:
                status = (*status, (int(150 * k), int(122 * k)))
        if busy:
            k = f.shape[0] / 1080
            status = (busy, "busy", (int(150 * k), int(122 * k)))
        ox, oy = self.stream.origin
        m.show((int(ox), int(oy), f.shape[1], f.shape[0]), slots, status)
        self.marked = True

    def _clear_marks(self):
        if getattr(self, "marked", False) and self.api._marks:
            self.api._marks.clear()
            self.marked = False

    def _sigs(self, f):
        h, w = f.shape[:2]
        k = h / 1080
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        out = {}
        for name, (x0, y0, x1, y1) in self.ROIS.items():
            roi = g[int(y0 * k):int(y1 * k), int(x0 * k):int(x1 * k)]
            out[name] = cv2.resize(roi, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32) if roi.size else None
        return out

    def run(self):
        while True:
            try:
                self._step()
            except Exception:
                logging.exception("auto read crashed")
                self.stream.stop()
                time.sleep(3.0)

    def _step(self):
        if not self.on:
            self.stream.stop()
            self.state = "off"
            time.sleep(1.0)
            return
        if self.api._lock.locked():                          # an F9 scan is running
            time.sleep(0.5)
            return
        try:
            f, why = self.stream.frame()
        except Exception as e:
            logging.warning("auto read: capture failed: %s", e)
            self.stream.stop()
            self.state = "capture failed"
            time.sleep(3.0)
            return
        if f is None:
            if why != self.state:
                logging.info("auto read: %s", why)
            self.state = why
            self._clear_marks()
            time.sleep(1.0)
            return
        self.state = "watching"
        now = time.time()
        due = False
        for name, s in self._sigs(f).items():
            if s is None:
                continue
            p = self.prev.get(name)
            self.prev[name] = s
            if p is None or float(np.abs(s - p).mean()) > 2.0:      # still moving
                self.since[name] = now
                if name == "stash":
                    self._clear_marks()                             # the slots moved: old marks are wrong
                continue
            if now - self.since.get(name, now) < 0.3:
                continue
            d = self.done.get(name)
            if d is None or float(np.abs(s - d).mean()) > 1.5:      # settled and not read like this yet
                due = True
        if not due or now - self.last_scan < 0.8:
            return
        for name in self.ROIS:
            self.done[name] = self.prev.get(name)
        self.last_scan = now
        if self.last_stash and now - self.last_stash < self.NEW_VISIT:
            self._status(f, "Reading…", "busy")              # still at the stash: say it's working
        with self.api._lock:
            scn = self.api._scn()
            t0 = time.time()
            res = scn.scan(f, auto=True, progress=lambda fr: self._show_marks(f, fr, None, "Reading…"))
            if res and res.get("kind") == "vault" and self.last_stash and now - self.last_stash > self.NEW_VISIT:
                # back at the stash after a while: read it from scratch (the cache keeps it fast)
                scn.reset_vault_session()
                res = scn.scan(f, auto=True)
        if not res:
            self._clear_marks()
            return
        frame = res.pop("frame", None)
        if res.get("kind") == "vault":
            self.last_stash = time.time()
            self._show_marks(f, frame, res)
        else:
            self._clear_marks()
        res["auto"] = True
        logging.info("auto read (%.1fs): %s", time.time() - t0,
                     {k: (v if k not in ("items", "quests", "unknowns", "categories") else len(v))
                      for k, v in res.items() if k not in ("debug",)})
        try:
            self.api._window.evaluate_js(f"window.onAutoScan&&window.onAutoScan({json.dumps(res)})")
        except Exception:
            logging.exception("auto read: sending the result to the page failed")


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
    api._mon = dict(mon)
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

    api._window.events.shown += lambda: api._exclude(api._window, "main")

    def on_start():
        start_hotkeys(api)
        try:
            api._marks = marks.Marks()
        except Exception:
            logging.exception("in-game marks unavailable")
        api._watcher = Watcher(api)
        api._watcher.start()
        if not a.no_update:
            threading.Thread(target=update_data, args=(api,), daemon=True).start()
    webview.start(on_start, private_mode=False, storage_path=DATA_DIR)


if __name__ == "__main__":
    main()
