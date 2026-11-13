"""Raider Logbook launcher (the entry point frozen into RaiderLogbook.exe).

1. removes Windows' "downloaded from the internet" mark so .NET can load pywebview's DLLs,
2. checks GitHub for newer *data* (the page template and hand-kept JSON listed in
   update/manifest.json) and saves it to %APPDATA%\\RaiderLogbook\\code,
3. starts the app bundled in the .exe.

It never downloads or runs program code: Python changes ship as a new .exe, and the app says when
one is available. Each launch writes %APPDATA%\\RaiderLogbook\\app.log.
"""
from __future__ import annotations

import logging
import os
import sys


def _unblock_downloaded_files():
    if os.name != "nt" or not hasattr(sys, "_MEIPASS"):
        return
    for root, _dirs, files in os.walk(os.path.dirname(sys.executable)):
        for f in files:
            if f.lower().endswith((".dll", ".exe", ".pyd")):
                try:
                    os.remove(os.path.join(root, f) + ":Zone.Identifier")
                except OSError:
                    pass


_unblock_downloaded_files()
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "RaiderLogbook")
os.makedirs(DATA_DIR, exist_ok=True)
LOG = os.path.join(DATA_DIR, "app.log")
try:
    if os.path.getsize(LOG) > 1_000_000:
        os.replace(LOG, LOG + ".old")
except OSError:
    pass
logging.basicConfig(filename=LOG, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
logging.info("---- launch: %s", sys.argv)

# record crashes: Python errors in any thread, and native crashes (OCR/window libraries) via faulthandler
import faulthandler  # noqa: E402
import threading  # noqa: E402
_crash = open(os.path.join(DATA_DIR, "crash.log"), "a", encoding="utf-8")
faulthandler.enable(_crash)
sys.excepthook = lambda t, v, tb: logging.critical("uncaught error", exc_info=(t, v, tb))
threading.excepthook = lambda a: logging.critical("uncaught error in thread %s", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

def _single_instance():
    """Two running copies both react to F9 and the one you look at may be an old version (it can be
    hidden with F8). On start, offer to close the copy that is already running."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    pid_file = os.path.join(DATA_DIR, "app.pid")
    try:
        old = int(open(pid_file).read().strip())
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid():
        h = k32.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, old)   # query info, terminate, synchronize
        if h:
            try:
                buf, n, code = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024), wintypes.DWORD()
                exe = os.path.basename(buf.value).lower() if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
                alive = k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value == 259     # STILL_ACTIVE
                if alive and (exe.startswith("python") or exe.startswith("raiderlogbook")):
                    ans = u32.MessageBoxW(None, "Raider Logbook is already running (its window may be hidden: "
                                          "press F8).\n\nClose it and start this one?", "Raider Logbook", 0x4 | 0x20 | 0x40000)
                    if ans != 6:                                    # IDYES
                        logging.info("another copy is running (pid %s); this one exits", old)
                        sys.exit(0)
                    k32.TerminateProcess(h, 0)
                    k32.WaitForSingleObject(h, 5000)
                    logging.info("closed the copy that was already running (pid %s)", old)
            finally:
                k32.CloseHandle(h)
    try:
        open(pid_file, "w").write(str(os.getpid()))
    except OSError:
        pass


try:
    _single_instance()
except SystemExit:
    raise
except Exception:
    logging.exception("single-instance check failed")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import updater  # noqa: E402

update = {"status": "skipped"}
if "--no-update" not in sys.argv:
    try:
        update = updater.check(DATA_DIR, log=logging.info)
    except Exception as e:                     # never block the app on the update
        logging.exception("update check failed")
        update = {"status": "failed", "error": str(e)}
logging.info("update: %s", update)
data_root, data_version = updater.code_root(DATA_DIR)
if data_root:
    os.environ["RAIDER_RES"] = data_root       # build.py reads web/app.html and sources/*.json from here

import app  # noqa: E402
app.main({"app": updater.APP_VERSION, "code": data_version, "update": update, "log": LOG})
