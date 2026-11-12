"""Raider Logbook launcher (the part frozen into RaiderLogbook.exe).

1. removes Windows' "downloaded from the internet" mark so .NET can load pywebview's DLLs,
2. checks GitHub for newer code (update/manifest.json) and downloads it to %APPDATA%\\RaiderLogbook\\code,
3. loads the app (app.py), scanner, capture and build.py from that folder by file path, falling back
   to the copies bundled in the .exe, and starts it.

Everything except this file and updater.py can therefore be fixed without a new .exe.
Each launch writes %APPDATA%\\RaiderLogbook\\app.log.
"""
from __future__ import annotations

import importlib.util
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import updater  # noqa: E402

update = {"status": "skipped"}
if "--no-update" not in sys.argv:
    try:
        update = updater.check(DATA_DIR, log=logging.info)
    except Exception as e:                     # never block the app on the update
        logging.exception("update check crashed")
        update = {"status": "failed", "error": str(e)}
logging.info("update: %s", update)

code_root, code_version = updater.code_root(DATA_DIR)
MODULES = [("scanner", "overlay/scanner.py"), ("capture", "overlay/capture.py"),
           ("build", "build.py"), ("app", "overlay/app.py")]
if code_root:
    os.environ["RAIDER_RES"] = code_root       # build.py reads web/ and sources/ from here
    try:
        for name, rel in MODULES:
            path = os.path.join(code_root, *rel.split("/"))
            if not os.path.exists(path):
                continue
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod            # later "import scanner" etc. get this copy
            spec.loader.exec_module(mod)
            logging.info("loaded %s from %s", name, path)
    except Exception as e:
        logging.exception("downloaded code failed to load, using the bundled copy")
        for name, _ in MODULES:
            sys.modules.pop(name, None)
        os.environ.pop("RAIDER_RES", None)
        code_root, code_version = None, updater.bundled_version()
        update = {"status": "failed", "error": f"downloaded code failed to load: {e}"}

if "app" not in sys.modules:
    import app  # noqa: E402,F401  (bundled copy; also tells PyInstaller to pack the app)
sys.modules["app"].main({"app": updater.APP_VERSION, "code": code_version, "update": update, "log": LOG})
