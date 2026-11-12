"""Self-update for Raider Logbook's code files.

The .exe bundles Python, the libraries and the scanner icons, which rarely change. The parts that
change often (the page template, build.py, the scanner, the hand-kept data in sources/) are listed
in update/manifest.json in the GitHub repo. On launch the app downloads newer versions of those
files into %APPDATA%\\RaiderLogbook\\code and uses them instead of the bundled copies, so small fixes
arrive without downloading a new .exe. "min_app" in the manifest says which .exe the files need;
when the .exe is too old the files are left alone and the app asks for a new download.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request

APP_VERSION = 1            # bump when the .exe itself must be rebuilt (new libraries, overlay.py changes)
REPO_RAW = "https://raw.githubusercontent.com/SiLo-xxxcxxcxyxxcyyy/riader-logbook/main/"
UA = {"User-Agent": "RaiderLogbook-updater"}


def _bundled_root():
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_manifest(root):
    try:
        return json.load(open(os.path.join(root, "update", "manifest.json"), encoding="utf-8"))
    except Exception:
        return None


def code_dir(data_dir):
    return os.path.join(data_dir, "code")


def activate(data_dir):
    """Put downloaded code ahead of the bundled code on sys.path. Returns the folder build.py should
    read web/ and sources/ from."""
    cd = code_dir(data_dir)
    local, bundled = _read_manifest(cd), _read_manifest(_bundled_root())
    if local and local.get("min_app", 1) <= APP_VERSION and local.get("version", 0) > (bundled or {}).get("version", 0):
        for p in (os.path.join(cd, "overlay"), cd):
            if p not in sys.path:
                sys.path.insert(0, p)
        os.environ["RAIDER_RES"] = cd
        return cd, local["version"]
    return _bundled_root(), (bundled or {}).get("version", 0)


def check(data_dir, log=print):
    """Download newer code files if there are any. Returns a dict describing what happened."""
    def get(path):
        with urllib.request.urlopen(urllib.request.Request(REPO_RAW + path, headers=UA), timeout=30) as r:
            return r.read()
    try:
        remote = json.loads(get("update/manifest.json"))
    except Exception as e:
        return {"status": "unreachable", "error": str(e)}
    cd = code_dir(data_dir)
    current = max((_read_manifest(cd) or {}).get("version", 0), (_read_manifest(_bundled_root()) or {}).get("version", 0))
    if remote.get("version", 0) <= current:
        return {"status": "current", "version": current}
    if remote.get("min_app", 1) > APP_VERSION:
        return {"status": "new_exe_needed", "version": remote["version"]}
    tmp = cd + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        for f in remote["files"] + ["update/manifest.json"]:
            dst = os.path.join(tmp, *f.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            data = get(f)
            if not data:
                raise ValueError(f"empty file {f}")
            open(dst, "wb").write(data)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"status": "failed", "error": str(e)}
    shutil.rmtree(cd, ignore_errors=True)
    os.replace(tmp, cd)
    log(f"code updated to version {remote['version']}")
    return {"status": "updated", "version": remote["version"], "from": current}
