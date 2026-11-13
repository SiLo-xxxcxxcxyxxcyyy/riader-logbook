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
